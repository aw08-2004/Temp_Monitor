// Shared search-as-you-type combobox.
//
// One widget behind every "start typing and pick a match" field in the console. Before
// this, the Backups restore browser had a real search box, the Permission Groups machine
// picker had a native <datalist> (no fuzzy match, free text accepted unvalidated), and
// the member picker had a bare email input with no suggestions at all. Roadmap #6 called
// for generalizing the one good precedent instead of writing a fourth one-off.
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
//   * The listbox is inserted next to the input, NOT appended to <body>. These pickers
//     live inside a <dialog>, which paints in the top layer above any position:fixed
//     element on the page -- a body-level dropdown would vanish behind the modal. Sitting
//     inside the input's own parent keeps it in the same layer.
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
        const emptyText = opts.emptyText || 'No matches';
        const renderItem = opts.renderItem || defaultRenderItem;
        const id = 'ac-list-' + (++widgetSeq);

        const parent = input.parentNode;
        // The listbox is absolutely positioned within the input's parent; make sure that
        // parent establishes a positioning context.
        if (getComputedStyle(parent).position === 'static') {
            parent.style.position = 'relative';
        }

        const list = el('ul', 'ac-list');
        list.id = id;
        list.setAttribute('role', 'listbox');
        list.hidden = true;
        parent.appendChild(list);

        input.setAttribute('role', 'combobox');
        input.setAttribute('aria-autocomplete', 'list');
        input.setAttribute('aria-expanded', 'false');
        input.setAttribute('aria-controls', id);
        input.setAttribute('autocomplete', 'off');

        let items = [];
        let active = -1;        // index of the keyboard-highlighted option
        let open = false;
        let requestSeq = 0;     // guards against out-of-order async results

        function position() {
            list.style.top = (input.offsetTop + input.offsetHeight + 2) + 'px';
            list.style.left = input.offsetLeft + 'px';
            list.style.width = input.offsetWidth + 'px';
        }

        function show() {
            if (open) return;
            open = true;
            position();
            list.hidden = false;
            input.setAttribute('aria-expanded', 'true');
        }

        function close() {
            if (!open) return;
            open = false;
            active = -1;
            list.hidden = true;
            input.setAttribute('aria-expanded', 'false');
            input.removeAttribute('aria-activedescendant');
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
})();
