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
// A turn belongs to its CHAT, not to the page. The hub keeps answering in its worker pool
// whatever the browser does, so leaving a chat that is mid-answer only stops this page
// LISTENING: the history shows a spinner on it, and opening it again renders what is stored
// and resumes polling the run from where the stored part ends. The first version stopped
// polling on a switch and never resumed, and the chat then refused every new message.
//
// Page context comes from the shell (window.parent.FleetShell.pageContext) and is sent with
// every message rather than stored, because "this PC" means whatever is on screen when the
// question is asked.
//
// IIFE-wrapped, classic script, no bundler -- same as every other file in static/js.
(function () {
    'use strict';

    const root = document.getElementById('assistant');
    if (!root) return;

    const $ = (id) => document.getElementById(id);
    const log = $('assistant-log');
    const thread = $('assistant-thread');
    const empty = $('assistant-empty');
    const form = $('assistant-form');
    const input = $('assistant-input');
    const sendBtn = $('assistant-send');
    const stopBtn = $('assistant-stop');
    const chatsEl = $('assistant-chats');
    const pinBtn = $('assistant-pin');
    const contextEl = $('assistant-context');
    const headingEl = $('assistant-heading');
    const offEl = $('assistant-off');
    const scrim = $('assistant-scrim');
    const historyToggle = $('assistant-history-toggle');

    const CHAT_KEY = 'tempmonitor:assistant:chat';
    const FOLD_KEY = 'tempmonitor:assistant:side';
    const POLL_MS = 800;
    const LIST_POLL_MS = 4000;
    // Below this the frame has no room for a history column: it is the docked panel, or a
    // narrow window, and the history becomes a drawer.
    const COMPACT_PX = 720;

    let chatId = null;
    let chats = [];
    let ready = true;
    // The run this page is LISTENING to. Always the open chat's; a run in another chat goes on
    // without anybody polling it.
    let run = null;          // { id, chatId, after, timer }
    let listTimer = null;
    let turn = null;         // the assistant turn being written to
    let thinking = null;
    // This conversation's command mode (hub 1.135.5). A conversation not created yet keeps
    // its chosen mode here and is created with it.
    let mode = 'ask';

    // ---------------- Storage (per-viewer conveniences only) ----------------
    function remember(key, value) {
        try {
            if (value) localStorage.setItem(key, value);
            else localStorage.removeItem(key);
        } catch (e) { /* private window: forget it */ }
    }

    function recall(key) {
        try { return localStorage.getItem(key); } catch (e) { return null; }
    }

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
        const label = pinned ? t('assistant.unpin') : t('assistant.pin');
        pinBtn.hidden = false;
        pinBtn.title = label;
        pinBtn.setAttribute('aria-label', label);
        pinBtn.setAttribute('aria-pressed', pinned ? 'true' : 'false');
    }

    pinBtn.addEventListener('click', () => {
        const s = shell();
        if (!s) return;
        if (s.isPinned()) s.unpin();
        else s.pin();
        applyWidth();
        showContext();
    });

    window.addEventListener('fleet:pagechange', () => { showContext(); applyWidth(); });

    // ---------------- Layout: history column or drawer ----------------
    function compact() {
        return root.classList.contains('asst--compact');
    }

    function setDrawer(open) {
        root.classList.toggle('asst--drawer', open);
        scrim.hidden = !open;
        historyToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    }

    function setFolded(folded) {
        root.classList.toggle('asst--folded', folded);
        remember(FOLD_KEY, folded ? 'folded' : null);
        historyToggle.setAttribute('aria-expanded', folded ? 'false' : 'true');
    }

    function applyWidth() {
        const narrow = root.clientWidth < COMPACT_PX;
        root.classList.toggle('asst--compact', narrow);
        if (narrow) {
            root.classList.remove('asst--folded');
            setDrawer(false);
        } else {
            setDrawer(false);
            setFolded(recall(FOLD_KEY) === 'folded');
        }
        // The toggle is only needed where the column is not already on screen.
        historyToggle.hidden = !narrow && !root.classList.contains('asst--folded');
        syncPin();
    }

    function toggleHistory() {
        if (compact()) setDrawer(!root.classList.contains('asst--drawer'));
        else setFolded(!root.classList.contains('asst--folded'));
        historyToggle.hidden = !compact() && !root.classList.contains('asst--folded');
    }

    historyToggle.addEventListener('click', toggleHistory);
    $('assistant-fold').addEventListener('click', toggleHistory);
    scrim.addEventListener('click', () => setDrawer(false));
    // Both, deliberately: a ResizeObserver callback runs in the browser's rendering step,
    // which a frame being moved into the dock may not reach before the operator looks; the
    // window resize event and the explicit calls after pin/unpin are the backstop.
    if (window.ResizeObserver) new ResizeObserver(applyWidth).observe(root);
    window.addEventListener('resize', applyWidth);

    // ---------------- Rendering ----------------
    /** A path on THIS hub, rebuilt from its parsed parts, or null. Parsed rather than only
     *  pattern-matched because `//host` and `/\host` both look relative and both leave the
     *  hub; the URL parser resolves them the way the browser will, and only a same-origin
     *  result is kept. The anchor gets the parser's own pathname, search and hash, never the
     *  string the text carried. */
    function hubPath(href) {
        if (!/^\/(?![/\\])/.test(href)) return null;
        let url;
        try {
            url = new URL(href, location.origin);
        } catch (e) {
            return null;
        }
        if (url.origin !== location.origin) return null;
        return url.pathname + url.search + url.hash;
    }

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
                // Rendered, not set as text: a model that bolds a machine name bolds its
                // LINK too (`**[PC-12](/machine/PC-12)**`), and as text the brackets and the
                // path showed on screen instead of a link.
                const strong = el('strong');
                renderInline(strong, match[2].slice(2, -2));
                parent.appendChild(strong);
            } else {
                const safe = hubPath(match[5]);
                if (safe) {
                    const a = el('a', null, match[4]);
                    a.setAttribute('href', safe);
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
        const bullet = /^\s*[-*]\s+(.*)$/;
        const numbered = /^\s*\d+[.)]\s+(.*)$/;
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
            // A table: a header row, a |---| separator, then rows. Models reach for one
            // whenever they compare things, and as text it was a wall of pipes.
            if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length
                    && /^\s*\|?\s*:?-{3,}/.test(lines[i + 1])) {
                const cells = (row) => row.trim().replace(/^\|/, '').replace(/\|$/, '')
                    .split('|').map((c) => c.trim());
                const table = el('table', 'asst__table');
                const head = el('tr');
                for (const cell of cells(line)) {
                    const th = el('th');
                    renderInline(th, cell);
                    head.appendChild(th);
                }
                const thead = el('thead');
                thead.appendChild(head);
                table.appendChild(thead);
                const tbody = el('tbody');
                i += 2;
                while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
                    const tr = el('tr');
                    for (const cell of cells(lines[i])) {
                        const td = el('td');
                        renderInline(td, cell);
                        tr.appendChild(td);
                    }
                    tbody.appendChild(tr);
                    i += 1;
                }
                table.appendChild(tbody);
                const scroll = el('div', 'asst__table-wrap');
                scroll.appendChild(table);
                out.appendChild(scroll);
                continue;
            }
            if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
                out.appendChild(el('hr'));
                i += 1;
                continue;
            }
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

    function markIcon() {
        const mark = el('div', 'asst__mark');
        mark.setAttribute('aria-hidden', 'true');
        const ns = 'http://www.w3.org/2000/svg';
        const svg = document.createElementNS(ns, 'svg');
        svg.setAttribute('viewBox', '0 0 24 24');
        svg.setAttribute('fill', 'none');
        svg.setAttribute('stroke', 'currentColor');
        svg.setAttribute('stroke-width', '2');
        const path = document.createElementNS(ns, 'path');
        path.setAttribute('d', 'M12 3l1.9 4.6L18.5 9.5l-4.6 1.9L12 16l-1.9-4.6L5.5 9.5l4.6-1.9z');
        svg.appendChild(path);
        mark.appendChild(svg);
        return mark;
    }

    /** The assistant's turn: its mark, then steps, cards and the answer underneath. One per
     *  operator message, so everything the model did for that message reads as one block. */
    function turnBody() {
        if (!turn) {
            empty.hidden = true;
            const node = el('div', 'asst__turn');
            node.appendChild(markIcon());
            turn = el('div', 'asst__turn-body');
            node.appendChild(turn);
            thread.appendChild(node);
        }
        return turn;
    }

    function keepThinkingLast() {
        if (thinking && turn) turn.appendChild(thinking);
    }

    function iconSvg(paths) {
        const ns = 'http://www.w3.org/2000/svg';
        const svg = document.createElementNS(ns, 'svg');
        svg.setAttribute('viewBox', '0 0 24 24');
        svg.setAttribute('fill', 'none');
        svg.setAttribute('stroke', 'currentColor');
        svg.setAttribute('stroke-width', '2');
        svg.setAttribute('aria-hidden', 'true');
        for (const d of paths) {
            const path = document.createElementNS(ns, 'path');
            path.setAttribute('d', d);
            svg.appendChild(path);
        }
        return svg;
    }

    const ICON_COPY = ['M9 9h11v11H9z', 'M5 15H4V4h11v1'];
    const ICON_CHECK = ['M5 12l5 5L20 7'];
    const ICON_PENCIL = ['M12 20h9', 'M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z'];
    const ICON_TRASH = ['M3 6h18', 'M8 6V4h8v2', 'M6 6l1 14h10l1-14'];

    /** The clipboard API first; the old textarea route where the frame was not granted it
     *  (a classic, unframed page in an older browser). Returns whether it worked. */
    async function copyText(text) {
        try {
            await navigator.clipboard.writeText(text);
            return true;
        } catch (e) {
            const area = el('textarea');
            area.value = text;
            area.setAttribute('readonly', '');
            area.style.position = 'fixed';
            area.style.opacity = '0';
            document.body.appendChild(area);
            area.select();
            let ok = false;
            try { ok = document.execCommand('copy'); } catch (err) { ok = false; }
            area.remove();
            return ok;
        }
    }

    /** A copy button for one message. `getText` is read at click time, so an answer copies
     *  the text as it reads on screen -- link labels, not the paths behind them. */
    function messageActions(getText) {
        const row = el('div', 'asst__msg-actions');
        const button = el('button', 'asst__icon asst__copy');
        button.type = 'button';
        const label = t('assistant.copy');
        button.title = label;
        button.setAttribute('aria-label', label);
        button.appendChild(iconSvg(ICON_COPY));
        button.addEventListener('click', async () => {
            if (!(await copyText(getText()))) {
                toast(t('assistant.error'), { kind: 'error' });
                return;
            }
            button.replaceChildren(iconSvg(ICON_CHECK));
            button.title = t('assistant.copied');
            button.setAttribute('aria-label', t('assistant.copied'));
            setTimeout(() => {
                button.replaceChildren(iconSvg(ICON_COPY));
                button.title = label;
                button.setAttribute('aria-label', label);
            }, 1500);
        });
        row.appendChild(button);
        return row;
    }

    /** Text the operator typed, or a hub note: always plain text. Kept apart from the
     *  markdown path so nothing typed into this page can reach the renderer -- one function
     *  switching on a role was flagged by CodeQL (code scanning #158). */
    function addUser(text) {
        empty.hidden = true;
        turn = null;
        const wrap = el('div', 'asst__user');
        const node = el('div', 'asst__msg--user');
        node.textContent = text;
        wrap.append(node, messageActions(() => text));
        thread.appendChild(wrap);
        scrollDown();
    }

    function toolsChip(tools) {
        return el('div', 'asst__tools', t('assistant.tools_used', { tools: tools.join(', ') }));
    }

    /** An answer, or a working step, from the server after render_links() built its links. */
    function addAnswer(content, { tools = [], step = false } = {}) {
        const body = turnBody();
        if (content) {
            const node = el('div', step ? 'asst__step' : 'asst__answer');
            node.appendChild(renderMarkdown(content));
            body.appendChild(node);
            // Answers only: a working step ("let me check the deployments") is not text
            // anybody wants to paste into a ticket.
            if (!step) body.appendChild(messageActions(() => node.innerText.trim()));
        }
        if (tools.length) body.appendChild(toolsChip(tools));
        keepThinkingLast();
        scrollDown();
    }

    function addPlain(className, text) {
        const node = el('div', className, text);
        turnBody().appendChild(node);
        keepThinkingLast();
        scrollDown();
    }

    function setThinking(text) {
        if (!text) {
            if (thinking) thinking.remove();
            thinking = null;
            return;
        }
        if (!thinking) {
            thinking = el('div', 'asst__thinking');
            const dots = el('span', 'asst__dots');
            dots.append(el('i'), el('i'), el('i'));
            thinking.append(dots, el('span'));
        }
        thinking.lastChild.textContent = text;
        turnBody().appendChild(thinking);
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
        const card = existing || el('div');
        card.className = `asst__action asst__action--${action.state}`;
        card.dataset.actionId = action.id;
        card.replaceChildren();
        let title;
        if (action.auto) title = t('assistant.action_auto_title', { mode: modeName(action.mode) });
        else if (action.machine) title = t('assistant.action_title_machine', { machine: action.machine });
        else title = t('assistant.action_title');
        card.appendChild(el('div', 'asst__action-title', title));
        if (action.impact || action.risk) {
            // The model's own judgement, as the hub stored it with the action: what it
            // said this would do, and how risky it called it. In Auto mode this is the
            // reasoning the action ran (or did not run) on.
            const judged = el('div', 'asst__action-impact');
            if (action.risk) {
                judged.appendChild(el('span', `asst__risk asst__risk--${action.risk === 'routine' ? 'routine' : 'critical'}`,
                    action.risk === 'routine' ? t('assistant.risk.routine') : t('assistant.risk.critical')));
            }
            if (action.impact) judged.appendChild(el('span', null, action.impact));
            card.appendChild(judged);
        }
        card.appendChild(el('div', 'asst__action-detail', actionDetail(action)));
        const state = el('div', 'stat-card__meta', t(STATE_KEYS[action.state] || 'assistant.state.pending'));
        if (action.result && action.result.status) state.textContent += ` (HTTP ${action.result.status})`;
        card.appendChild(state);

        if (action.state === 'pending') {
            const buttons = el('div', 'asst__action-buttons');
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
            buttons.append(confirm, reject);
            card.appendChild(buttons);
        }
        if (!existing) {
            turnBody().appendChild(card);
            keepThinkingLast();
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
                body: JSON.stringify(Object.assign({ context: pageContext() },
                                                   typed ? { typed: typed.value } : {})),
            });
            const data = await response.json().catch(() => ({}));
            if (data.action) renderAction(data.action, card);
            // The hub starts a follow-up turn after a confirmed action, so the model reads
            // the outcome and reports it without being asked.
            if (data.run_id) listen(data.run_id, -1);
            if (!response.ok) {
                toast(data.error || t('assistant.error'), { kind: 'error' });
                if (!data.action) for (const button of card.querySelectorAll('button')) button.disabled = false;
            }
        } catch (e) {
            toast(t('assistant.error'), { kind: 'error' });
            for (const button of card.querySelectorAll('button')) button.disabled = false;
        }
    }

    // ---------------- History ----------------
    // A rename in progress. The list is not redrawn under it: a re-render would throw the
    // half-typed name away (the list re-reads itself while another chat is answering).
    let renaming = false;

    function rowButton(className, label, paths, onClick) {
        const button = el('button', `asst__icon ${className}`);
        button.type = 'button';
        button.title = label;
        button.setAttribute('aria-label', label);
        button.appendChild(iconSvg(paths));
        button.addEventListener('click', onClick);
        return button;
    }

    /** Swap the row's title for a text box. Enter or leaving it saves; Escape cancels. */
    function startRename(row, chat, open) {
        renaming = true;
        const box = el('input', 'input asst__rename');
        box.value = chat.title || '';
        box.maxLength = 80;
        box.setAttribute('aria-label', t('assistant.rename'));
        row.replaceChild(box, open);
        box.focus();
        box.select();
        let finished = false;
        const finish = async (save) => {
            if (finished) return;
            finished = true;
            const title = box.value.trim();
            if (save && title && title !== chat.title) {
                try {
                    const response = await fetch(`/api/assistant/chats/${encodeURIComponent(chat.id)}`, {
                        method: 'PATCH',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ title }),
                    });
                    if (!response.ok) throw new Error(String(response.status));
                } catch (e) {
                    toast(t('assistant.error'), { kind: 'error' });
                }
            }
            renaming = false;
            await loadChats();
        };
        box.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); finish(true); }
            if (e.key === 'Escape') { e.preventDefault(); finish(false); }
        });
        box.addEventListener('blur', () => finish(true));
    }

    const deleteDialog = $('assistant-delete-dialog');

    /** The console's own modal instead of the browser's confirm(). Resolves to whether the
     *  operator chose Delete; Escape and Cancel both resolve false.
     *
     *  Decided on the form's `submit` (which carries the button pressed, synchronously) and the
     *  dialog's `cancel` (Escape), not on `close`: `close` is queued as a later task, and a
     *  first version that waited for it never deleted anything in a frame the browser was
     *  not drawing. Both listeners come off again, so a dialog opened twice answers once. */
    function confirmDelete(chat) {
        return new Promise((resolve) => {
            const form = deleteDialog.querySelector('form');
            $('assistant-delete-body').textContent = t('assistant.delete_body',
                { title: chat.title || t('assistant.untitled') });
            const done = (ok) => {
                form.removeEventListener('submit', onSubmit);
                deleteDialog.removeEventListener('cancel', onCancel);
                resolve(ok);
            };
            const onSubmit = (e) => done(!!(e.submitter && e.submitter.value === 'delete'));
            const onCancel = () => done(false);
            form.addEventListener('submit', onSubmit);
            deleteDialog.addEventListener('cancel', onCancel);
            deleteDialog.showModal();
            $('assistant-delete-confirm').focus();
        });
    }

    function renderChats() {
        if (renaming) return;
        chatsEl.replaceChildren();
        if (!chats.length) {
            chatsEl.appendChild(el('div', 'asst__chats-empty', t('assistant.no_chats')));
            return;
        }
        for (const chat of chats) {
            const row = el('div', 'asst__chat' + (chat.id === chatId ? ' asst__chat--active' : ''));
            const open = el('button', 'asst__chat-open', chat.title || t('assistant.untitled'));
            open.type = 'button';
            open.title = chat.title || t('assistant.untitled');
            open.addEventListener('click', () => {
                setDrawer(false);
                if (chat.id !== chatId) openChat(chat.id);
            });
            row.appendChild(open);
            if (chat.running) {
                const spin = el('span', 'asst__spinner');
                spin.title = t('assistant.chat_running');
                spin.setAttribute('aria-label', t('assistant.chat_running'));
                row.appendChild(spin);
            }
            row.appendChild(rowButton('asst__chat-action', t('assistant.rename'), ICON_PENCIL,
                                      () => startRename(row, chat, open)));
            row.appendChild(rowButton('asst__chat-action', t('assistant.delete_chat'), ICON_TRASH,
                                      () => deleteChat(chat)));
            chatsEl.appendChild(row);
        }
    }

    function setHeading() {
        const chat = chats.find((c) => c.id === chatId);
        headingEl.textContent = (chat && chat.title) || t('assistant.heading');
    }

    /** The history, and whether any chat other than the open one is still answering. While
     *  one is, the list is re-read every few seconds so its spinner stops when it is done. */
    async function loadChats() {
        try {
            const response = await fetch('/api/assistant/chats');
            const data = await response.json();
            chats = data.chats || [];
        } catch (e) {
            chats = [];
        }
        renderChats();
        setHeading();
        clearTimeout(listTimer);
        if (chats.some((c) => c.running && c.id !== chatId)) {
            listTimer = setTimeout(loadChats, LIST_POLL_MS);
        }
    }

    function clearThread() {
        for (const node of [...thread.children]) if (node !== empty) node.remove();
        empty.hidden = false;
        turn = null;
        thinking = null;
    }

    async function openChat(id) {
        stopListening();
        clearThread();
        chatId = id || null;
        // A new conversation starts in Ask, always (owner's decision): a Bypass chosen for
        // one job must not carry into the next.
        mode = 'ask';
        renderMode();
        remember(CHAT_KEY, chatId);
        renderChats();
        setHeading();
        if (!chatId) return;
        let data;
        try {
            const response = await fetch(`/api/assistant/chats/${encodeURIComponent(chatId)}`);
            if (!response.ok) throw new Error('gone');
            data = await response.json();
        } catch (e) {
            chatId = null;
            remember(CHAT_KEY, null);
            renderChats();
            return;
        }
        if (chatId !== id) return;      // another chat was opened meanwhile
        mode = (data.chat && data.chat.mode) || 'ask';
        renderMode();
        const items = [
            ...data.messages.map((m) => ({ at: m.created_at, m })),
            ...data.actions.map((a) => ({ at: a.created_at, a })),
        ].sort((x, y) => x.at - y.at);
        for (const item of items) {
            if (item.a) renderAction(item.a);
            else if (item.m.role === 'user') addUser(item.m.content);
            else if (item.m.role === 'assistant') {
                addAnswer(item.m.content, { tools: item.m.tools, step: item.m.step });
            } else addPlain('asst__step', item.m.content);
        }
        if (data.run) listen(data.run.id, data.run.seq);
        scrollDown();
    }

    async function deleteChat(chat) {
        if (!(await confirmDelete(chat))) return;
        const id = chat.id;
        try {
            const response = await fetch(`/api/assistant/chats/${encodeURIComponent(id)}`,
                                         { method: 'DELETE' });
            // 404 is "already gone", which is what the operator asked for. Anything else
            // failed, and the conversation is still there: keep it open and say so.
            if (!response.ok && response.status !== 404) throw new Error(String(response.status));
        } catch (e) {
            toast(t('assistant.error'), { kind: 'error' });
            return;
        }
        if (id === chatId) await openChat(null);
        await loadChats();
    }

    function newChat() {
        setDrawer(false);
        openChat(null);
        input.focus();
    }

    $('assistant-new').addEventListener('click', newChat);
    $('assistant-new-compact').addEventListener('click', newChat);

    // ---------------- Listening to a run ----------------
    function busy(on) {
        sendBtn.hidden = on;
        stopBtn.hidden = !on;
    }

    /** Stop polling. The run itself goes on in the hub; see the file comment. */
    function stopListening() {
        if (run) clearTimeout(run.timer);
        run = null;
        busy(false);
        setThinking(null);
    }

    function listen(runId, after) {
        stopListening();
        run = { id: runId, chatId, after, timer: null };
        busy(true);
        setThinking(t('assistant.thinking'));
        run.timer = setTimeout(poll, 300);
    }

    async function poll() {
        const current = run;
        if (!current) return;
        let data;
        try {
            const response = await fetch(`/api/assistant/runs/${current.id}?after_seq=${current.after}`);
            data = await response.json();
            if (!response.ok) throw new Error(data.error || 'poll');
        } catch (e) {
            if (run !== current) return;
            addPlain('asst__error', t('assistant.error'));
            stopListening();
            return;
        }
        // The operator switched chats while this request was out: its events belong to a
        // chat no longer on screen, and will be read from storage when that chat is opened.
        if (run !== current) return;
        for (const event of data.events || []) {
            current.after = Math.max(current.after, event.seq);
            if (event.type === 'text') {
                addAnswer(event.content, { step: !event.final });
            } else if (event.type === 'tool') {
                if (event.action) renderAction(event.action);
                if (event.state === 'started') {
                    setThinking(t('assistant.using_tool', { tool: event.name }));
                } else {
                    setThinking(t('assistant.thinking'));
                }
            } else if (event.type === 'error') {
                addPlain('asst__error',
                    event.error === 'stopped' ? t('assistant.stopped') : event.error);
            } else if (event.type === 'title') {
                // The model named the conversation (assistant_web.name_it): show it now
                // rather than at the end of the turn.
                const chat = chats.find((c) => c.id === current.chatId);
                if (chat) chat.title = event.title;
                renderChats();
                setHeading();
            }
        }
        if (data.done) {
            stopListening();
            await loadChats();
            // The conversation's name is chosen beside the answer and can land just after it.
            // One more look, only while the title is still the first-message placeholder.
            const chat = chats.find((c) => c.id === current.chatId);
            if (chat && chat.title_source === 'auto') setTimeout(loadChats, 2500);
            return;
        }
        current.timer = setTimeout(poll, POLL_MS);
    }

    async function send(text) {
        text = String(text || '').trim();
        if (!text || run || !ready) return;
        busy(true);
        try {
            if (!chatId) {
                const created = await fetch('/api/assistant/chats', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ mode }),
                });
                const chat = await created.json();
                if (!created.ok) throw new Error(chat.error || 'create');
                chatId = chat.id;
                remember(CHAT_KEY, chatId);
            }
            addUser(text);
            input.value = '';
            autosize();
            const response = await fetch(`/api/assistant/chats/${encodeURIComponent(chatId)}/messages`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ text, context: pageContext() }),
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
                addPlain('asst__error', data.error || t('assistant.error'));
                busy(false);
                return;
            }
            listen(data.run_id, -1);
            loadChats();
        } catch (e) {
            addPlain('asst__error', t('assistant.error'));
            busy(false);
        }
    }

    function autosize() {
        input.style.height = 'auto';
        input.style.height = `${input.scrollHeight}px`;
    }

    input.addEventListener('input', autosize);

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
        if (!run) return;
        fetch(`/api/assistant/runs/${run.id}/cancel`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
        }).catch(() => toast(t('assistant.error'), { kind: 'error' }));
    });

    for (const button of document.querySelectorAll('.asst__example')) {
        button.addEventListener('click', () => send(button.textContent));
    }

    // ---------------- Command mode ----------------
    const modeBtn = $('assistant-mode');
    const modeMenu = $('assistant-mode-menu');
    const modeDialog = $('assistant-mode-dialog');
    const MODE_NAMES = {
        ask: () => t('assistant.mode.ask'),
        auto: () => t('assistant.mode.auto'),
        bypass: () => t('assistant.mode.bypass'),
    };
    const MODE_WARNINGS = {
        auto: () => t('assistant.mode.warn_auto'),
        bypass: () => t('assistant.mode.warn_bypass'),
    };

    function modeName(value) {
        return (MODE_NAMES[value] || MODE_NAMES.ask)();
    }

    function renderMode() {
        $('assistant-mode-label').textContent = modeName(mode);
        modeBtn.className = `asst__mode asst__mode--${mode}`;
        for (const option of modeMenu.querySelectorAll('[data-mode]')) {
            option.setAttribute('aria-checked', option.dataset.mode === mode ? 'true' : 'false');
        }
    }

    function setMenu(open) {
        modeMenu.hidden = !open;
        modeBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    }

    /** The warning for leaving Ask. Decided on the form's submit, like the delete dialog,
     *  and nothing changes unless the operator presses the switch button. */
    function confirmMode(target) {
        return new Promise((resolve) => {
            const form = modeDialog.querySelector('form');
            $('assistant-mode-warning').textContent = MODE_WARNINGS[target]();
            $('assistant-mode-confirm').textContent = t('assistant.mode.warn_accept',
                                                        { mode: modeName(target) });
            const done = (ok) => {
                form.removeEventListener('submit', onSubmit);
                modeDialog.removeEventListener('cancel', onCancel);
                resolve(ok);
            };
            const onSubmit = (e) => done(!!(e.submitter && e.submitter.value === 'switch'));
            const onCancel = () => done(false);
            form.addEventListener('submit', onSubmit);
            modeDialog.addEventListener('cancel', onCancel);
            modeDialog.showModal();
        });
    }

    async function chooseMode(target) {
        setMenu(false);
        if (target === mode) return;
        if (target !== 'ask' && !(await confirmMode(target))) return;
        if (chatId) {
            try {
                const response = await fetch(`/api/assistant/chats/${encodeURIComponent(chatId)}`, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ mode: target }),
                });
                if (!response.ok) throw new Error(String(response.status));
            } catch (e) {
                toast(t('assistant.error'), { kind: 'error' });
                return;
            }
        }
        mode = target;
        renderMode();
        input.focus();
    }

    modeBtn.addEventListener('click', () => setMenu(modeMenu.hidden));
    for (const option of modeMenu.querySelectorAll('[data-mode]')) {
        option.addEventListener('click', () => chooseMode(option.dataset.mode));
    }
    document.addEventListener('click', (e) => {
        if (!modeMenu.hidden && !e.target.closest('.asst__mode-wrap')) setMenu(false);
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && !modeMenu.hidden) { setMenu(false); modeBtn.focus(); }
    });

    // ---------------- Boot ----------------
    async function boot() {
        renderMode();
        applyWidth();
        showContext();
        try {
            const response = await fetch('/api/assistant/status');
            const status = await response.json();
            if (!status.ready) {
                ready = false;
                offEl.hidden = false;
                offEl.textContent = t('assistant.off', { reason: status.error || '' });
                sendBtn.disabled = true;
                input.disabled = true;
            }
        } catch (e) { /* the first send will say what is wrong */ }
        await loadChats();
        const saved = recall(CHAT_KEY);
        if (saved && chats.some((c) => c.id === saved)) await openChat(saved);
    }

    boot();
})();
