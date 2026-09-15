// Ctrl+K: find a device or a page, from anywhere in the console (hub 1.112.0).
//
// WHY. Every route to one PC used to go through Asset Inventory: open Inventory, type, click
// the name, click the tab. An operator answering a call knows the PC's name or its asset tag
// before they know which page they want, so the search belongs to the chrome, not to a page.
// From any page: Ctrl+K, "042", Right Right, Enter -> PC-042's Files tab.
//
// WHERE IT RUNS. In the shell document (and in a classic, frameless page), never inside a
// frame. A keystroke inside a frame never reaches the shell's document, so shell.js calls
// listen() on each framed document as it loads -- same pattern as its link interception.
// Navigation is a click on a real <a href> in THIS document, which shell.js already routes
// into the frame; in a classic page the same click is an ordinary navigation. So there is one
// navigation path, not a second one to keep in step with the shell.
//
// WHAT IT SEARCHES.
//   * Devices: /api/machines, the same scoped roster Inventory and the machine switcher use
//     (access.filter_rows), so the palette cannot enumerate a machine the operator could not
//     already see. Matched on the same fields Inventory searches -- a match on something the
//     operator cannot see anywhere would look like a bug.
//   * Pages: read off the rendered sidebar, not a second list. The sidebar is already
//     capability-gated server-side, so a page the operator may not open is not offered, and a
//     renamed nav entry cannot drift from what the palette calls it.
//
// NOT STOLEN FROM: a terminal (xterm owns Ctrl+K as "kill to end of line" in most shells) and
// the remote viewer, which forwards every key to the PC and calls preventDefault on it. Both
// are caught by the defaultPrevented / .xterm guard in isShortcut().
//
// Rejected: matching on the server. The roster is already small enough that Inventory filters
// it client-side (roadmap #6), and a request per keystroke would make the palette feel slower
// than the Inventory box it is meant to beat.
//
// Built with createElement/textContent, never innerHTML: machine names, serials and OS labels
// are whatever a host reported to the unauthenticated /api/report.
(function () {
    'use strict';

    const dialog = document.getElementById('palette');
    const input = document.getElementById('palette-input');
    const results = document.getElementById('palette-results');
    const opener = document.getElementById('palette-open');
    if (!dialog || !input || !results) return;

    // Keep in step with inventory.js SEARCH_FIELDS; tests/test_command_palette.py pins it.
    const SEARCH_FIELDS = ['machine', 'asset_tag', 'serial_number', 'service_tag',
                           'manufacturer', 'os_label'];
    // Tab slugs a device result can jump straight to, in the order ArrowRight walks them.
    // The machine page drops tabs the operator or the device cannot use, and tabs.js falls
    // back to Overview for a slug it does not have -- so offering Files here to someone
    // without issue_commands lands them on Overview, not on a 403.
    const ACTIONS = [
        { slug: null, label: () => t('palette.action_overview') },
        { slug: 'terminal', label: () => t('tools.tab.terminal') },
        { slug: 'files', label: () => t('tools.tab.files') },
        { slug: 'network', label: () => t('tools.tab.network') },
    ];
    const MAX_DEVICES = 12;
    const MAX_PAGES = 6;
    // Long enough that opening the palette twice in a minute costs one request, short enough
    // that a PC that just came online shows as online.
    const ROSTER_TTL_MS = 30_000;

    let roster = null;          // [{ row, haystack }]
    let rosterAt = 0;
    let rosterError = '';
    let loading = false;
    let options = [];           // [{ kind: 'device'|'page', href, row?, el }]
    let active = 0;
    let action = 0;

    // ---------------- Data ----------------
    async function loadRoster() {
        if (loading) return;
        loading = true;
        try {
            const response = await fetch('/api/machines', { credentials: 'same-origin' });
            if (!response.ok) {
                throw new Error(t('common.request_failed', { url: '/api/machines', status: response.status }));
            }
            const rows = await response.json();
            roster = rows.map((row) => ({
                row,
                haystack: SEARCH_FIELDS.map((f) => String(row[f] || '').toLowerCase()),
            }));
            rosterAt = Date.now();
            rosterError = '';
        } catch (e) {
            rosterError = e.message;
        } finally {
            loading = false;
            if (dialog.open) render();
        }
    }

    function pages() {
        const sidebar = document.getElementById('app-sidebar');
        if (!sidebar) return [];
        return Array.from(sidebar.querySelectorAll('a.sidebar__link[href]')).map((link) => ({
            label: (link.querySelector('.sidebar__link-label')?.textContent || '').trim(),
            href: link.getAttribute('href'),
            icon: link.querySelector('svg'),
        })).filter((p) => p.label);
    }

    /** Lower is better; null is no match. Name prefix beats name substring beats any other
     *  field, so typing the start of a hostname puts that PC first even when forty serials
     *  happen to contain the same digits. */
    function score(entry, q) {
        const [name] = entry.haystack;
        if (name.startsWith(q)) return 0;
        if (name.includes(q)) return 1;
        if (entry.haystack.some((field, i) => i > 0 && field.includes(q))) return 2;
        return null;
    }

    // ---------------- Rendering ----------------
    function heading(text) {
        const h = document.createElement('div');
        h.className = 'px-2 pt-2 pb-1 text-[11px] font-semibold uppercase tracking-wider text-muted';
        h.setAttribute('role', 'presentation');
        h.textContent = text;
        return h;
    }

    function skeleton() {
        // A few grey bars where the rows will be, rather than a sentence: the palette opens
        // in a frame of the eye, and text that turns into rows reads as the list jumping.
        const wrap = document.createElement('div');
        wrap.className = 'flex flex-col gap-2 p-2';
        wrap.setAttribute('aria-label', t('palette.loading'));
        for (let i = 0; i < 3; i++) {
            const bar = document.createElement('div');
            bar.className = 'h-9 animate-pulse rounded-lg bg-control';
            wrap.appendChild(bar);
        }
        return wrap;
    }

    function hrefFor(row, slug) {
        const path = `/machine/${encodeURIComponent(row.machine)}`;
        return slug ? `${path}?tab=${slug}` : path;
    }

    function deviceOption(row, index) {
        const option = document.createElement('div');
        option.id = `palette-opt-${index}`;
        option.setAttribute('role', 'option');
        option.className = 'flex cursor-pointer items-center gap-3 rounded-lg px-3 py-2';

        const dot = document.createElement('span');
        dot.className = row.status === 'online'
            ? 'size-2 shrink-0 rounded-full bg-success'
            : 'size-2 shrink-0 rounded-full bg-muted';

        const text = document.createElement('a');
        text.className = 'flex min-w-0 flex-1 flex-col no-underline';
        text.href = hrefFor(row, null);
        text.tabIndex = -1;
        const name = document.createElement('span');
        name.className = 'truncate font-medium text-text';
        name.textContent = row.machine;
        const meta = document.createElement('span');
        meta.className = 'truncate text-xs text-muted';
        meta.textContent = [row.asset_tag, row.serial_number, row.os_label].filter(Boolean).join(' · ');
        text.append(name, meta);

        // The jump-to-tab links, only drawn on the highlighted row (see highlight()). Real
        // links, so a mouse can use them as well as ArrowLeft/Right.
        const actions = document.createElement('span');
        actions.className = 'hidden shrink-0 items-center gap-1';
        actions.dataset.actions = '';
        ACTIONS.forEach((a, i) => {
            const link = document.createElement('a');
            link.href = hrefFor(row, a.slug);
            link.tabIndex = -1;
            link.dataset.action = String(i);
            link.className = 'rounded-md px-2 py-0.5 text-xs text-muted no-underline hover:text-text';
            link.textContent = a.label();
            actions.appendChild(link);
        });

        option.append(dot, text, actions);
        return option;
    }

    function pageOption(page, index) {
        const option = document.createElement('a');
        option.id = `palette-opt-${index}`;
        option.setAttribute('role', 'option');
        option.href = page.href;
        option.tabIndex = -1;
        option.className = 'flex cursor-pointer items-center gap-3 rounded-lg px-3 py-2 text-text no-underline';
        if (page.icon) {
            const icon = page.icon.cloneNode(true);
            icon.setAttribute('class', 'size-4 shrink-0 text-muted');
            icon.setAttribute('aria-hidden', 'true');
            option.appendChild(icon);
        }
        const label = document.createElement('span');
        label.className = 'truncate';
        label.textContent = page.label;
        option.appendChild(label);
        return option;
    }

    function render() {
        const q = input.value.trim().toLowerCase();
        const nodes = [];
        options = [];

        // Devices first: they are what this exists for.
        if (!roster && !rosterError) {
            nodes.push(heading(t('palette.devices')), skeleton());
        } else if (rosterError) {
            const err = document.createElement('p');
            err.className = 'px-3 py-2 text-sm text-danger';
            err.textContent = rosterError;
            nodes.push(heading(t('palette.devices')), err);
        } else {
            let matches;
            if (q) {
                matches = roster
                    .map((entry) => ({ entry, s: score(entry, q) }))
                    .filter((m) => m.s !== null)
                    .sort((a, b) => a.s - b.s || a.entry.row.machine.localeCompare(b.entry.row.machine))
                    .map((m) => m.entry.row);
            } else {
                // Nothing typed: the machines you can act on right now, alphabetically.
                matches = roster.map((e) => e.row)
                    .sort((a, b) => (a.status === 'online' ? 0 : 1) - (b.status === 'online' ? 0 : 1)
                        || a.machine.localeCompare(b.machine));
            }
            matches = matches.slice(0, MAX_DEVICES);
            if (matches.length) {
                nodes.push(heading(t('palette.devices')));
                for (const row of matches) {
                    const el = deviceOption(row, options.length);
                    options.push({ kind: 'device', row, el });
                    nodes.push(el);
                }
            }
        }

        const pageMatches = pages()
            .filter((p) => !q || p.label.toLowerCase().includes(q))
            .slice(0, MAX_PAGES);
        if (pageMatches.length) {
            nodes.push(heading(t('palette.pages')));
            for (const page of pageMatches) {
                const el = pageOption(page, options.length);
                options.push({ kind: 'page', href: page.href, el });
                nodes.push(el);
            }
        }

        if (!options.length && roster) {
            const empty = document.createElement('p');
            empty.className = 'px-3 py-6 text-center text-sm text-muted';
            empty.textContent = t('palette.empty', { query: input.value.trim() });
            nodes.push(empty);
        }

        results.replaceChildren(...nodes);
        options.forEach((opt, i) => {
            opt.el.addEventListener('mousemove', () => { if (active !== i) highlight(i, 0); });
            opt.el.addEventListener('click', (e) => {
                // A click on one of the row's own links goes where that link goes; anywhere
                // else on the row means "open the highlighted choice".
                if (e.target.closest('a')) { closeSoon(); return; }
                e.preventDefault();
                highlight(i, 0);
                go();
            });
        });
        highlight(Math.min(active, Math.max(options.length - 1, 0)), 0);
    }

    function highlight(index, actionIndex) {
        active = index;
        action = actionIndex;
        options.forEach((opt, i) => {
            const on = i === active;
            opt.el.classList.toggle('bg-accent-soft', on);
            opt.el.setAttribute('aria-selected', String(on));
            const actions = opt.el.querySelector('[data-actions]');
            if (actions) {
                actions.classList.toggle('hidden', !on);
                actions.classList.toggle('flex', on);
                for (const link of actions.querySelectorAll('[data-action]')) {
                    const current = on && Number(link.dataset.action) === action;
                    link.classList.toggle('bg-control', current);
                    link.classList.toggle('text-text', current);
                }
            }
        });
        const opt = options[active];
        if (opt) {
            input.setAttribute('aria-activedescendant', opt.el.id);
            opt.el.scrollIntoView({ block: 'nearest' });
        } else {
            input.removeAttribute('aria-activedescendant');
        }
    }

    // ---------------- Acting ----------------
    function closeSoon() {
        // After the click has been dispatched, so shell.js sees the link before the dialog
        // (and the link inside it) goes away.
        setTimeout(() => { if (dialog.open) dialog.close(); }, 0);
    }

    function go() {
        const opt = options[active];
        if (!opt) return;
        let link;
        if (opt.kind === 'device') {
            link = opt.el.querySelector(`[data-action="${action}"]`);
        } else {
            link = opt.el;
        }
        if (!link) return;
        dialog.close();
        // A real click on a real link in this document: shell.js routes it into the frame,
        // and a classic page simply follows it.
        link.click();
    }

    input.addEventListener('input', () => { active = 0; render(); });
    input.addEventListener('keydown', (e) => {
        const count = options.length;
        if (e.key === 'ArrowDown' && count) {
            e.preventDefault();
            highlight((active + 1) % count, 0);
        } else if (e.key === 'ArrowUp' && count) {
            e.preventDefault();
            highlight((active - 1 + count) % count, 0);
        } else if ((e.key === 'ArrowRight' || e.key === 'ArrowLeft') && options[active]?.kind === 'device') {
            // Only steal the arrows from the text box when the caret is at the end: someone
            // editing "PC-04" in the middle still needs Left to move the caret.
            if (e.key === 'ArrowLeft' && action === 0) return;
            if (e.key === 'ArrowRight' && input.selectionStart !== input.value.length) return;
            e.preventDefault();
            const step = e.key === 'ArrowRight' ? 1 : -1;
            highlight(active, Math.min(Math.max(action + step, 0), ACTIONS.length - 1));
        } else if (e.key === 'Enter') {
            e.preventDefault();
            go();
        }
    });

    // Clicking the dimmed backdrop closes it: a click on the dialog element itself (rather
    // than on anything inside it) can only have landed on the ::backdrop.
    dialog.addEventListener('click', (e) => {
        if (e.target === dialog) dialog.close();
    });

    function open() {
        if (dialog.open) {
            input.focus();
            input.select();
            return;
        }
        input.value = '';
        active = 0;
        action = 0;
        if (!roster || Date.now() - rosterAt > ROSTER_TTL_MS) loadRoster();
        render();
        dialog.showModal();
        input.focus();
    }

    /** Ctrl+K / Cmd+K, unless something on the page already owns the keystroke. */
    function isShortcut(e) {
        if (e.defaultPrevented || e.altKey || e.shiftKey) return false;
        if (!(e.ctrlKey || e.metaKey) || (e.key || '').toLowerCase() !== 'k') return false;
        const target = e.target;
        if (target && target.closest && target.closest('.xterm')) return false;
        return true;
    }

    function onKey(e) {
        if (!isShortcut(e)) return;
        e.preventDefault();
        open();
    }

    if (opener) opener.addEventListener('click', open);
    document.addEventListener('keydown', onKey);

    window.FleetPalette = {
        open,
        isShortcut,
        /** Hear the shortcut inside another same-origin document (a shell frame). Idempotent
         *  per document; a frame navigation brings a new document, and the shell calls this
         *  again on its load. */
        listen(doc) {
            if (!doc || doc.__fleetPalette) return;
            doc.__fleetPalette = true;
            doc.addEventListener('keydown', onKey);
        },
    };
})();
