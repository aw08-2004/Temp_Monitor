// One Leaflet map, shared by the machine page's Location fold and the fleet map page.
//
// Both draw the same thing from the same payload shape, so the drawing lives here once rather
// than twice with a slow divergence between them. What differs is only how many fixes go in
// and what happens when one is clicked, and both are parameters.
//
// Three rules govern this file:
//
//   1. **The map must never imply more precision than the fix had.** Every point is drawn with
//      its accuracy radius as a real circle in metres beside it, and a fix the device called
//      `stale` is drawn in a different colour and says how old it is. A marker alone on a
//      street map is a claim that somebody will act on.
//   2. **No innerHTML anywhere near a payload.** A popup carries a machine name, which is
//      arbitrary text reported by a remote machine over an unauthenticated endpoint. Popups
//      are built with createElement and textContent, exactly as the rest of the console does.
//   3. **Leaflet measures its container at construction.** One built inside a folded
//      `<details>` or a hidden tab panel comes back zero pixels tall and stays that way -- the
//      same defect machine.js already handles for Chart.js. `invalidate()` exists for the
//      callers that know when their container became visible, and calling it when nothing
//      changed is free.
(function () {
    'use strict';

    // Deliberately not from the theme tokens. A tile map is somebody else's imagery and does
    // not follow this console's light/dark switch, so a marker coloured from --accent would be
    // invisible on half the maps it lands on. These two are chosen to read on street tiles.
    const FRESH = '#e11d48';    // a fix the device took just now
    const STALE = '#6b7280';    // a last known position, which is a different claim

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function ago(epoch) {
        if (!epoch) return '';
        const seconds = Math.max(0, Math.floor(Date.now() / 1000 - epoch));
        if (seconds < 90) return t('location.age.moments');
        if (seconds < 5400) return t('location.age.minutes', { count: Math.round(seconds / 60) });
        if (seconds < 172800) return t('location.age.hours', { count: Math.round(seconds / 3600) });
        return t('location.age.days', { count: Math.round(seconds / 86400) });
    }

    /**
     * Build a map inside `host`.
     *
     * `config` is the `map` block the location API serves: {tile_url, attribution, zoom}. It
     * comes from the server rather than being hardcoded here so that a site with no internet
     * egress can point at its own tile server -- see hub/settings.py's map.* section.
     */
    function create(host, config, options) {
        const settings = config || {};
        const opts = options || {};

        const map = L.map(host, {
            // The zoom control is kept; the layers control is not, and that is what keeps
            // leaflet.css's images/ references unreachable. See static/vendor/README.md.
            zoomControl: true,
            attributionControl: true,
        }).setView([0, 0], 2);

        if (settings.tile_url) {
            L.tileLayer(settings.tile_url, {
                attribution: settings.attribution || '',
                maxZoom: 19,
            }).addTo(map);
        } else {
            // No tile source configured. Points still plot and still carry their coordinates,
            // which is most of the value for "which of these two buildings" -- and saying so
            // beats a field of grey squares that reads as a broken page.
            host.classList.add('map--tileless');
        }

        let layer = L.layerGroup().addTo(map);

        function popup(fix) {
            const box = el('div', 'map-popup');
            box.appendChild(el('strong', null, fix.machine));

            const when = el('div', 'stat-card__meta',
                fix.stale
                    ? t('location.popup.last_known', { age: ago(fix.fixed_at || fix.received_at) })
                    : t('location.popup.fixed', { age: ago(fix.fixed_at || fix.received_at) }));
            box.appendChild(when);

            if (fix.accuracy_m) {
                box.appendChild(el('div', 'stat-card__meta',
                    t('location.popup.accuracy', { metres: Math.round(fix.accuracy_m) })));
            }
            if (fix.provider) {
                box.appendChild(el('div', 'stat-card__meta',
                    t('location.popup.provider', { provider: fix.provider })));
            }
            // The coordinates in full, always. They are what somebody pastes into a phone's
            // own map app when they walk out of the door to go and find the device, and an
            // operator on a hub with no tile source has nothing else at all.
            box.appendChild(el('div', 'stat-card__meta map-popup__coords',
                `${fix.lat.toFixed(5)}, ${fix.lon.toFixed(5)}`));

            if (opts.onOpen) {
                const link = el('button', 'btn btn--ghost', t('location.popup.open'));
                link.type = 'button';
                link.addEventListener('click', () => opts.onOpen(fix));
                box.appendChild(link);
            }
            return box;
        }

        /** Replace everything on the map with `fixes`, and frame them. */
        function show(fixes) {
            layer.clearLayers();
            const points = (fixes || []).filter(
                (f) => typeof f.lat === 'number' && typeof f.lon === 'number');
            if (points.length === 0) return false;

            const bounds = [];
            points.forEach((fix) => {
                const colour = fix.stale ? STALE : FRESH;
                // The accuracy circle FIRST, so the point sits on top of it rather than
                // under. A radius the device did not state is not drawn at all -- an assumed
                // one would be a number the console invented.
                if (fix.accuracy_m) {
                    L.circle([fix.lat, fix.lon], {
                        radius: fix.accuracy_m,
                        color: colour, weight: 1, opacity: 0.5,
                        fillColor: colour, fillOpacity: 0.12,
                        interactive: false,
                    }).addTo(layer);
                }
                const marker = L.circleMarker([fix.lat, fix.lon], {
                    radius: 7, color: '#fff', weight: 2,
                    fillColor: colour, fillOpacity: 1,
                }).addTo(layer);
                marker.bindPopup(popup(fix));
                // The name on permanent display for a fleet of several; pointless and noisy
                // for one, which already has its name above the map.
                if (points.length > 1) {
                    marker.bindTooltip(fix.machine, { permanent: true, direction: 'right',
                                                      offset: [8, 0] });
                }
                bounds.push([fix.lat, fix.lon]);
            });

            if (points.length === 1) {
                map.setView(bounds[0], settings.zoom || 16);
            } else {
                // padded, because a marker exactly on the edge is half off it, and its
                // accuracy circle is mostly off it.
                map.fitBounds(bounds, { padding: [40, 40], maxZoom: settings.zoom || 16 });
            }
            return true;
        }

        /**
         * Re-measure. Leaflet reads its container's size once, at construction: one built
         * inside a folded <details> or an inactive tab panel is 0 px tall forever, and the
         * symptom is a map that renders as a thin grey line rather than an error.
         */
        function invalidate() {
            // The deferral is not superstition. `toggle` on a <details> fires while the
            // browser is still laying the newly-open content out, so a synchronous measure
            // reads the size the container had a frame ago -- which for a fold opening from
            // closed is zero, exactly the value this call exists to correct.
            requestAnimationFrame(() => map.invalidateSize());
        }

        return { map, show, invalidate };
    }

    window.LocationMap = { create };
})();
