// The fleet map (roadmap #23 phase C): every device in scope that has a known position.
//
// **A last-known-position map, and the page has to say so.** Nothing here is live. Each point
// is where a device was the last time an operator asked, which for most devices is never and
// for the rest is whenever somebody last needed to know. A map that looked live would be read
// as live, so the age of every fix is on screen and stale ones are drawn differently.
//
// **The list beside the map is not decoration.** It is what works when the tile server is
// unreachable, what an operator reads on a narrow screen, and what carries the coordinates
// somebody pastes into a phone before walking out of the door.
(function () {
    'use strict';

    const host = document.getElementById('fleet-map');
    if (!host) return;

    const listEl = document.getElementById('fleet-map-list');
    const emptyEl = document.getElementById('fleet-map-empty');
    const countEl = document.getElementById('fleet-map-count');
    const filterEl = document.getElementById('fleet-map-filter');

    let view = null;
    let fixes = [];

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function when(epoch) {
        return epoch ? new Date(epoch * 1000).toLocaleString() : '';
    }

    function visible() {
        const needle = (filterEl.value || '').trim().toLowerCase();
        if (!needle) return fixes;
        return fixes.filter((f) => (f.machine || '').toLowerCase().includes(needle));
    }

    function renderList(rows) {
        listEl.replaceChildren();
        rows.forEach((fix) => {
            const row = el('button', 'fleet-map__row');
            row.type = 'button';
            row.appendChild(el('span', 'fleet-map__name', fix.machine));
            row.appendChild(el('span', 'stat-card__meta',
                fix.stale ? t('location.list.last_known', { when: when(fix.fixed_at) })
                          : t('location.list.fixed', { when: when(fix.fixed_at) })));
            row.appendChild(el('span', 'stat-card__meta map-popup__coords',
                `${fix.lat.toFixed(5)}, ${fix.lon.toFixed(5)}`));
            // Clicking a row frames that device rather than navigating away: the question the
            // page answers is "where are they", and losing the map to answer "which one is
            // this" would be the wrong trade.
            row.addEventListener('click', () => {
                if (view) view.show([fix]);
            });
            listEl.appendChild(row);
        });
    }

    function draw() {
        const rows = visible();
        countEl.textContent = t('location.map.count', { count: rows.length });
        renderList(rows);
        const drew = view ? view.show(rows) : false;
        // The empty state distinguishes "nothing matches your filter" from "no device in your
        // scope has ever been located", which are different problems with different answers.
        emptyEl.hidden = drew;
        emptyEl.textContent = fixes.length === 0 ? t('location.map.none')
                                                 : t('location.map.no_match');
    }

    async function load() {
        let response;
        try {
            response = await fetch('/api/location/fleet');
        } catch (e) {
            emptyEl.textContent = t('location.map.unreachable');
            emptyEl.hidden = false;
            return;
        }
        if (!response.ok) {
            emptyEl.textContent = t('location.map.unreachable');
            emptyEl.hidden = false;
            return;
        }
        const data = await response.json();
        fixes = data.fixes || [];
        // Built after the first payload, because the tile URL is in it -- a map created before
        // the fetch would need its layer swapped afterwards for no gain.
        if (!view) {
            view = window.LocationMap.create(host, data.map || {}, {
                // A popup's "Open" goes to the device's own page, which is where its history,
                // its Locate button and everything else about it lives. A plain assignment
                // rather than an <a>: shell.js intercepts link clicks to route them through
                // the frame, and a link built inside a Leaflet popup is outside the DOM it
                // watches, so it would navigate the frame out of the shell.
                onOpen: (fix) => {
                    window.location.href = `/machine/${encodeURIComponent(fix.machine)}`;
                },
            });
        }
        draw();
    }

    filterEl.addEventListener('input', draw);
    load();
})();
