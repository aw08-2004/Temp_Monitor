// Shared search-as-you-type combobox.
//
// One widget behind every "start typing and pick a match" field in the console. Before
// this, the Backups restore browser had a real search box, the Permission Groups machine
// picker had a native <datalist> (no fuzzy match, free text accepted unvalidated), and
// the member picker had a bare email input with no suggestions at all. Roadmap #6 called
// for generalizing the one good precedent instead of writing a fourth one-off.
//
// It now covers the whole console, not just the two Permission Groups pickers: base.html
// loads this on every page, and a document-level sweep upgrades long <select>s
// (enhanceSelect) and <datalist>-bound inputs (upgradeDatalistInput) automatically, so a
// dropdown added later gets search without being wired up by hand. See those two sections
// at the bottom of the file.
//
// Usage:
//   const ac = attachAutocomplete(inputEl, {
//       source: (query) => [{ value, label, sublabel }],   // sync array or a Promise of one
//       onSelect: (item) => { ... },                       // a match was chosen
//       minChars: 0,          // show suggestions once the query is this long (0 = on focus)
//       emptyText: 'No matches',
//       renderItem: (item, query) => Node,                 // optional custom option body
//   });
//   ac.close();  ac.destroy();
//
// Design notes:
//   * The listbox is portaled to the nearest enclosing <dialog>, or to <body> when the
//     input is not in one, and positioned in viewport coordinates (position: fixed).
//     Both halves matter. It cannot simply go to <body>: a modal <dialog> paints in the
//     top layer above everything on the page, so a body-level dropdown would vanish
//     behind it. It cannot stay next to the input either: .modal__body scrolls
//     (`overflow-y: auto`), and an absolutely positioned child of a scroller is clipped
//     to it -- which cropped the remote picker's dropdown to a single sliver of a row.
//     Fixed positioning escapes ancestor overflow; landing inside the <dialog> keeps it
//     in the modal's layer.
//   * Nodes are built with textContent/createElement, never innerHTML: the suggestions are
//     operator- and agent-supplied strings (hostnames, display names) re-rendered live.
//   * Async sources are race-guarded by a monotonic request id, so a slow response for an
//     old query can't overwrite the results of the current one.

(function () {
    let widgetSeq = 0;

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    // Default option renderer: a bold label with an optional muted sublabel underneath.
    function defaultRenderItem(item) {
        const wrap = el('span', 'ac-option__body');
        wrap.appendChild(el('span', 'ac-option__label', item.label != null ? item.label : item.value));
        if (item.sublabel) {
            wrap.appendChild(el('span', 'ac-option__sub', item.sublabel));
        }
        return wrap;
    }

    window.attachAutocomplete = function attachAutocomplete(input, options) {
        const opts = options || {};
        const minChars = opts.minChars != null ? opts.minChars : 0;
        const emptyText = opts.emptyText || t('common.no_matches');
        const renderItem = opts.renderItem || defaultRenderItem;
        const id = 'ac-list-' + (++widgetSeq);

        // See the design note at the top of the file for why the list is hosted here
        // rather than beside the input.
        const host = input.closest('dialog') || document.body;

        const list = el('ul', 'ac-list');
        list.id = id;
        list.setAttribute('role', 'listbox');
        list.hidden = true;
        host.appendChild(list);

        input.setAttribute('role', 'combobox');
        input.setAttribute('aria-autocomplete', 'list');
        input.setAttribute('aria-expanded', 'false');
        input.setAttribute('aria-controls', id);
        input.setAttribute('autocomplete', 'off');

        let items = [];
        let active = -1;        // index of the keyboard-highlighted option
        let open = false;
        let requestSeq = 0;     // guards against out-of-order async results

        const GAP = 2;          // breathing room between the field and the list
        const EDGE = 8;         // and between the list and the viewport edge
        const MIN_HEIGHT = 120; // below this, flipping to the other side is worth it

        function position() {
            const box = input.getBoundingClientRect();
            const below = window.innerHeight - box.bottom - GAP - EDGE;
            const above = box.top - GAP - EDGE;
            list.style.left = box.left + 'px';
            list.style.width = box.width + 'px';
            // Drop down by default, but flip above the field when the space below is too
            // cramped to be useful and there is more of it up top -- a picker sitting near
            // the bottom of a short modal has almost nothing underneath it.
            if (below < MIN_HEIGHT && above > below) {
                list.style.top = '';
                list.style.bottom = (window.innerHeight - box.top + GAP) + 'px';
                list.style.maxHeight = Math.max(MIN_HEIGHT, above) + 'px';
            } else {
                list.style.bottom = '';
                list.style.top = (box.bottom + GAP) + 'px';
                list.style.maxHeight = Math.max(MIN_HEIGHT, below) + 'px';
            }
        }

        // Viewport coordinates go stale the moment anything moves, and the list is no
        // longer a sibling that moves with the field. Capture-phase so scrolling of any
        // ancestor (a modal body, the page) is seen, not just the window's own.
        function reposition() { if (open) position(); }

        function show() {
            if (open) return;
            open = true;
            position();
            list.hidden = false;
            input.setAttribute('aria-expanded', 'true');
            window.addEventListener('scroll', reposition, true);
            window.addEventListener('resize', reposition);
        }

        function close() {
            if (!open) return;
            open = false;
            active = -1;
            list.hidden = true;
            input.setAttribute('aria-expanded', 'false');
            input.removeAttribute('aria-activedescendant');
            window.removeEventListener('scroll', reposition, true);
            window.removeEventListener('resize', reposition);
        }

        function setActive(index) {
            const nodes = list.querySelectorAll('.ac-option');
            if (!nodes.length) return;
            active = (index + nodes.length) % nodes.length;
            nodes.forEach((node, i) => {
                const on = i === active;
                node.classList.toggle('is-active', on);
                node.setAttribute('aria-selected', on ? 'true' : 'false');
                if (on) {
                    input.setAttribute('aria-activedescendant', node.id);
                    node.scrollIntoView({ block: 'nearest' });
                }
            });
        }

        function choose(index) {
            if (index < 0 || index >= items.length) return;
            const item = items[index];
            close();
            if (opts.onSelect) opts.onSelect(item);
        }

        function renderList(query) {
            list.replaceChildren();
            if (!items.length) {
                const empty = el('li', 'ac-empty', emptyText);
                empty.setAttribute('aria-disabled', 'true');
                list.appendChild(empty);
                active = -1;
                show();
                return;
            }
            items.forEach((item, i) => {
                const li = el('li', 'ac-option');
                li.id = id + '-opt-' + i;
                li.setAttribute('role', 'option');
                li.setAttribute('aria-selected', 'false');
                li.appendChild(renderItem(item, query));
                // mousedown, not click: the input's blur (which closes the list) fires
                // before a click would, so mousedown is the event that still lands.
                li.addEventListener('mousedown', (e) => { e.preventDefault(); choose(i); });
                list.appendChild(li);
            });
            active = -1;
            show();
        }

        async function query() {
            // The list no longer rides along with the input's parent, so a page that
            // re-renders its markup without calling destroy() would strand it. Cheapest
            // place to notice is the next query.
            if (!input.isConnected) { close(); list.remove(); return; }
            const q = input.value.trim();
            if (q.length < minChars) { close(); return; }
            const mySeq = ++requestSeq;
            let result;
            try {
                result = await Promise.resolve(opts.source(q));
            } catch (e) {
                result = [];
            }
            if (mySeq !== requestSeq) return;   // a newer query superseded this one
            items = Array.isArray(result) ? result : [];
            renderList(q);
        }

        input.addEventListener('input', query);
        input.addEventListener('focus', () => {
            // Re-open on focus if there is something to show (or minChars allows empty).
            if (input.value.trim().length >= minChars) query();
        });
        input.addEventListener('blur', () => {
            // Delay so a mousedown on an option is processed before we tear the list down.
            setTimeout(close, 120);
        });
        input.addEventListener('keydown', (e) => {
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                if (!open) { query(); return; }
                setActive(active + 1);
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                setActive(active - 1);
            } else if (e.key === 'Enter') {
                if (open && active >= 0) {
                    e.preventDefault();
                    choose(active);
                }
                // With nothing highlighted, Enter falls through to the caller's own
                // handler (e.g. "add exactly what I typed").
            } else if (e.key === 'Escape') {
                if (open) { e.preventDefault(); close(); }
            }
        });

        return {
            close,
            refresh: query,
            destroy() {
                close();
                list.remove();
            },
        };
    };

    // ------------------------------------------------------------ searchable <select>
    //
    // Same combobox, wrapped around a native <select> so every long dropdown in the
    // console gets type-to-filter instead of a scroll-hunt through 200 hostnames.
    //
    // The native <select> stays in the DOM and stays the source of truth: callers keep
    // reading `select.value`, writing `select.value = x`, rebuilding `<option>`s and
    // listening for 'change' exactly as before. This only bolts a search box on top --
    // that is what makes it safe to apply automatically (see enhanceSelectsIn below)
    // without auditing every page that renders a dropdown.
    //
    //   * A picked option sets the select's value and dispatches a bubbling 'change',
    //     so existing handlers fire as if the user had used the native control.
    //   * A MutationObserver re-syncs the visible text when the page replaces the
    //     options or assigns a new value programmatically (neither fires an event).
    //   * Blur restores the selected option's label, so a half-typed query never sits
    //     in the box looking like a selection that was never made.
    //   * Disabled and hidden selects, and any select carrying data-no-search, are
    //     skipped; multi-selects are out of scope (chips, not a single-value box).

    function optionItems(select, query) {
        const q = (query || '').trim().toLowerCase();
        const out = [];
        Array.prototype.forEach.call(select.options, (opt, index) => {
            if (opt.disabled) return;
            const label = opt.textContent.trim();
            const group = opt.parentNode && opt.parentNode.tagName === 'OPTGROUP'
                ? opt.parentNode.label : '';
            const hay = (label + ' ' + opt.value + ' ' + group).toLowerCase();
            if (q && !hay.includes(q)) return;
            out.push({ value: opt.value, label: label || opt.value, sublabel: group, index });
        });
        return out;
    }

    window.enhanceSelect = function enhanceSelect(select, options) {
        if (!select || select.multiple || select.dataset.acEnhanced) return null;
        select.dataset.acEnhanced = '1';
        const opts = options || {};

        const wrap = el('div', 'select-search');
        select.parentNode.insertBefore(wrap, select);
        wrap.appendChild(select);

        const input = el('input', 'select-search__input ' + (select.className || 'select'));
        input.type = 'text';
        input.placeholder = opts.placeholder || select.dataset.searchPlaceholder || 'Search…';
        // The native control still owns the value, but it must not be a second tab stop
        // or a second announcement of the same field.
        select.classList.add('select-search__native');
        select.setAttribute('tabindex', '-1');
        select.setAttribute('aria-hidden', 'true');
        if (select.id) {
            const label = document.querySelector(`label[for="${CSS.escape(select.id)}"]`);
            if (label) input.setAttribute('aria-label', label.textContent.trim());
        }
        wrap.insertBefore(input, select);

        function selectedLabel() {
            const opt = select.options[select.selectedIndex];
            return opt ? opt.textContent.trim() : '';
        }

        function syncFromSelect() {
            if (document.activeElement !== input) input.value = selectedLabel();
            input.disabled = select.disabled;
        }

        const ac = attachAutocomplete(input, {
            minChars: 0,
            emptyText: opts.emptyText || t('common.no_matches'),
            source: (q) => optionItems(select, q),
            onSelect: (item) => {
                select.selectedIndex = item.index;
                input.value = selectedLabel();
                select.dispatchEvent(new Event('change', { bubbles: true }));
                if (opts.onSelect) opts.onSelect(item);
            },
        });

        // Clicking the box should behave like opening a dropdown: show everything and
        // let the first keystroke replace the current label rather than append to it.
        input.addEventListener('focus', () => input.select());
        input.addEventListener('blur', () => setTimeout(syncFromSelect, 130));

        const observer = new MutationObserver(syncFromSelect);
        observer.observe(select, { childList: true, subtree: true, attributes: true,
                                   attributeFilter: ['value', 'disabled'] });
        select.addEventListener('change', syncFromSelect);
        syncFromSelect();

        return {
            sync: syncFromSelect,
            destroy() {
                observer.disconnect();
                ac.destroy();
                input.remove();
                select.classList.remove('select-search__native');
                select.removeAttribute('tabindex');
                select.removeAttribute('aria-hidden');
                delete select.dataset.acEnhanced;
                wrap.parentNode.insertBefore(select, wrap);
                wrap.remove();
            },
        };
    };

    // ------------------------------------------------------------ <datalist> upgrade
    //
    // The other half of the console's dropdowns are free-text inputs bound to a
    // <datalist> (deploy targets, audit actors, restore/preview machines). A datalist
    // only prefix-matches, renders no secondary detail and looks different in every
    // browser. This swaps in the same combobox: substring matching anywhere in the
    // value, our own styling, and free text still accepted because the input is a plain
    // text input underneath.
    //
    // The <datalist> element stays in the DOM under its original id -- pages populate it
    // by id and keep doing so -- but the input's `list` binding is dropped so the native
    // popup does not fight ours. Options are read at query time, so a list filled by a
    // later fetch needs no re-wiring.

    function upgradeDatalistInput(input) {
        const listId = input.getAttribute('list');
        const datalist = listId && document.getElementById(listId);
        if (!datalist) return null;
        input.removeAttribute('list');
        input.dataset.acDatalist = listId;

        return attachAutocomplete(input, {
            minChars: 0,
            emptyText: input.dataset.emptyText || t('common.no_matches_free_text'),
            source: (q) => {
                const query = q.toLowerCase();
                return Array.prototype.map.call(datalist.options, (opt) => ({
                    value: opt.value,
                    label: opt.value,
                    sublabel: opt.label && opt.label !== opt.value ? opt.label : '',
                }))
                    .filter((item) => !query || item.value.toLowerCase().includes(query)
                                              || item.sublabel.toLowerCase().includes(query))
                    .slice(0, 50);
            },
            onSelect: (item) => {
                input.value = item.value;
                // Pages that filter as you type listen for 'input'; some also watch
                // 'change'. A pick is a user edit, so emit both.
                input.dispatchEvent(new Event('input', { bubbles: true }));
                input.dispatchEvent(new Event('change', { bubbles: true }));
                input.focus();
            },
        });
    }

    // Sweep a subtree and make every dropdown that is long enough to be worth searching
    // searchable. Opt in early with data-search on the element, opt out with
    // data-no-search; otherwise the option count decides.
    const AUTO_SEARCH_MIN_OPTIONS = 8;

    window.enhanceSelectsIn = function enhanceSelectsIn(root) {
        const scope = root || document;
        if (!scope.querySelectorAll) return;
        scope.querySelectorAll('select').forEach((select) => {
            if (select.dataset.acEnhanced || select.dataset.noSearch !== undefined) return;
            if (select.multiple) return;
            const opted = select.dataset.search !== undefined;
            if (!opted && select.options.length < AUTO_SEARCH_MIN_OPTIONS) return;
            enhanceSelect(select);
        });
        scope.querySelectorAll('input[list]').forEach((input) => {
            if (input.dataset.acDatalist || input.dataset.noSearch !== undefined) return;
            upgradeDatalistInput(input);
        });
    };

    // Most of these dropdowns are filled by JS after load, and several grow as the fleet
    // does -- so one pass at DOMContentLoaded would miss them. Watch the document and
    // re-sweep on the next frame after any DOM change, which also coalesces the burst of
    // mutations a render produces into a single pass.
    function watchForSelects() {
        window.enhanceSelectsIn(document);
        let scheduled = false;
        new MutationObserver(() => {
            if (scheduled) return;
            scheduled = true;
            requestAnimationFrame(() => {
                scheduled = false;
                window.enhanceSelectsIn(document);
            });
        }).observe(document.body, { childList: true, subtree: true });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', watchForSelects);
    } else {
        watchForSelects();
    }
})();
