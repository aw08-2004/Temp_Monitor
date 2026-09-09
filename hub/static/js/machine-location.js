// The machine page's Location fold (roadmap #23 phase C): where this device was when somebody
// last asked, and a button to ask again.
//
// **Nothing here polls, and that is the feature rather than an optimisation.** Every other
// live card on this page refreshes on a timer because the machine is reporting anyway; a
// location exists only because an operator pressed a button, so a refresh loop would either
// show the same fix forever or -- far worse -- imply the console was watching. The payload is
// fetched when the fold opens, and again when an answer is expected.
//
// **The fold is hidden entirely for a device that cannot locate.** Not disabled, not greyed:
// absent. Every Windows PC in the fleet reports no `locate` feature (see hub/capabilities.py's
// asymmetry -- an unreported FEATURE is a no, unlike an unreported command), and a permanently
// dead card on several hundred machine pages is worse than no card.
(function () {
    'use strict';

    const fold = document.getElementById('card-location');
    if (!fold || !window.MachineContext) return;

    const machine = window.MachineContext.current();
    if (!machine) return;
    const mapHost = document.getElementById('location-map');
    const statusEl = document.getElementById('location-status');
    const detailEl = document.getElementById('location-detail');
    const historyEl = document.getElementById('location-history');
    const button = document.getElementById('locate-now');

    let view = null;
    let loaded = false;
    // Set while a locate is in flight, so the poll below stops on its own rather than running
    // for the life of the page.
    let waiting = null;

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function when(epoch) {
        return epoch ? new Date(epoch * 1000).toLocaleString() : '';
    }

    function renderHistory(rows) {
        historyEl.replaceChildren();
        if (!rows || rows.length === 0) return;
        const table = el('table', 'data-table');
        const head = el('thead');
        const headRow = el('tr');
        [t('location.history.when'), t('location.history.outcome'),
         t('location.history.who')].forEach((label) => headRow.appendChild(el('th', null, label)));
        head.appendChild(headRow);
        table.appendChild(head);

        const body = el('tbody');
        rows.forEach((row) => {
            const tr = el('tr');
            tr.appendChild(el('td', null, when(row.received_at)));
            // A switch of literal keys rather than a computed one: tests/test_i18n.py can only
            // scan for literals, so a status whose translation was never written would ship
            // silently and render its own key.
            let outcome;
            if (row.status === 'located') {
                outcome = row.stale ? t('location.outcome.located_stale')
                                    : t('location.outcome.located');
            } else if (row.status === 'unavailable') {
                outcome = t('location.outcome.unavailable');
            } else {
                outcome = t('location.outcome.no_answer');
            }
            // The device's own words after the outcome, when it gave any. That sentence is the
            // difference between "turn location on" and "walk outside", and dropping it would
            // leave three statuses doing the work of a dozen reasons.
            const cell = el('td', null, row.detail ? `${outcome} -- ${row.detail}` : outcome);
            tr.appendChild(cell);
            tr.appendChild(el('td', null, row.requested_by || ''));
            body.appendChild(tr);
        });
        table.appendChild(body);
        historyEl.appendChild(table);
    }

    function render(data) {
        // Presentation only; the endpoints re-decide. Hidden rather than disabled for the
        // reason in the file header.
        fold.hidden = !data.supported;
        if (!data.supported) return;

        button.hidden = !data.can_locate;
        const pending = Boolean(data.pending);
        button.disabled = pending;
        button.textContent = pending ? t('location.locating') : t('location.locate_now');

        if (data.latest) {
            if (!view) {
                view = window.LocationMap.create(mapHost, data.map || {});
                // Built while the fold may have been closed, so measure once now that it
                // holds something.
                view.invalidate();
            }
            mapHost.hidden = false;
            view.show([Object.assign({ machine }, data.latest)]);
            view.invalidate();
            statusEl.textContent = data.latest.stale
                ? t('location.status.last_known', { when: when(data.latest.fixed_at) })
                : t('location.status.fixed', { when: when(data.latest.fixed_at) });
            const bits = [];
            if (data.latest.accuracy_m) {
                bits.push(t('location.popup.accuracy',
                            { metres: Math.round(data.latest.accuracy_m) }));
            }
            if (data.latest.provider) {
                bits.push(t('location.popup.provider', { provider: data.latest.provider }));
            }
            bits.push(`${data.latest.lat.toFixed(5)}, ${data.latest.lon.toFixed(5)}`);
            detailEl.textContent = bits.join(' · ');
        } else {
            mapHost.hidden = true;
            statusEl.textContent = pending ? t('location.status.waiting')
                                           : t('location.status.never');
            detailEl.textContent = '';
        }

        renderHistory(data.history);

        // The poll exists only while an answer is outstanding, and stops the moment one
        // arrives. A device that is switched off never answers, so the hub's own sweep is what
        // eventually files a no-answer row -- this just stops asking.
        if (pending && !waiting) {
            waiting = setInterval(load, 4000);
        } else if (!pending && waiting) {
            clearInterval(waiting);
            waiting = null;
        }
    }

    async function load() {
        let response;
        try {
            response = await fetch(`/api/location/machines/${encodeURIComponent(machine)}`);
        } catch (e) {
            return;
        }
        if (!response.ok) {
            // 403 is the ordinary answer for an operator outside scope, and the fold simply
            // does not exist for them.
            fold.hidden = true;
            return;
        }
        loaded = true;
        render(await response.json());
    }

    button.addEventListener('click', async () => {
        button.disabled = true;
        try {
            const response = await fetch(
                `/api/location/machines/${encodeURIComponent(machine)}`,
                { method: 'POST', headers: { 'Content-Type': 'application/json' },
                  body: '{}' });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
                // Rendered verbatim: a refusal here is either "this device cannot locate",
                // which names the machine and its platform, or a permission answer. Both are
                // more useful than anything this file could say instead.
                statusEl.textContent = data.error || t('location.failed');
                button.disabled = false;
                return;
            }
            render(data);
        } catch (e) {
            statusEl.textContent = t('location.failed');
            button.disabled = false;
        }
    });

    // Fetched when the fold opens rather than on page load: a machine page that is mostly
    // charts should not spend a request on a card nobody has looked at. `loaded` keeps a
    // reopen from re-fetching, while the map still gets re-measured every time -- which is the
    // half that actually has to happen, because Leaflet read a zero height while it was shut.
    fold.addEventListener('toggle', () => {
        if (!fold.open) return;
        if (!loaded) load();
        if (view) view.invalidate();
    });

    if (fold.open) load();
})();
