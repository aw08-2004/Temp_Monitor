// The machine page's Apps fold (roadmap #23 phase D): what this device says is installed.
//
// **System apps are hidden by default and findable on demand.** A phone reports 150-400
// packages and perhaps thirty of them are things somebody installed. Showing all of them first
// buries the thirty that answer the question an operator came with; showing only the thirty
// hides the browser, the store and the camera, which are exactly the ones a helpdesk is asked
// about. So: user apps by default, a checkbox to include the rest, and a filter over both.
//
// **`suspended` and `disabled` are what the DEVICE says, never what a policy asked for.** That
// is the whole reason those columns exist. A policy reported as applied while three of its
// targets are still running is worse than no policy, and this table is where the difference
// becomes visible.
//
// Like the Location fold, the fold is hidden entirely on a device that has never reported an
// inventory -- which is every Windows PC -- and nothing here polls: an app list changes when
// somebody installs something, not on a timer.
(function () {
    'use strict';

    const fold = document.getElementById('card-apps');
    if (!fold || !window.MachineContext) return;

    const machine = window.MachineContext.current();
    if (!machine) return;

    const bodyEl = document.getElementById('apps-body');
    const countEl = document.getElementById('apps-count');
    const filterEl = document.getElementById('apps-filter');
    const systemEl = document.getElementById('apps-system');
    const statusEl = document.getElementById('apps-status');

    let inventory = null;
    let loaded = false;

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function visible() {
        if (!inventory) return [];
        const needle = (filterEl.value || '').trim().toLowerCase();
        return inventory.apps.filter((app) => {
            if (!systemEl.checked && app.is_system) return false;
            if (!needle) return true;
            // Both fields, because an operator arrives with either: a label they read on the
            // device, or a package name they copied out of a policy.
            return (app.label || '').toLowerCase().includes(needle)
                || (app.package || '').toLowerCase().includes(needle);
        });
    }

    function stateLabel(app) {
        // A switch of literal keys rather than one built by concatenation: tests/test_i18n.py
        // can only scan for literals, so a computed key whose translation was never written
        // would ship silently and render itself.
        if (app.suspended) return t('apps.state.suspended');
        if (!app.enabled) return t('apps.state.disabled');
        return t('apps.state.installed');
    }

    function draw() {
        const rows = visible();
        bodyEl.replaceChildren();
        countEl.textContent = t('apps.count', {
            shown: rows.length, total: inventory ? inventory.counts.total : 0,
        });

        if (rows.length === 0) {
            bodyEl.appendChild(el('p', 'stat-card__meta',
                inventory && inventory.counts.total
                    ? t('apps.no_match') : t('apps.none')));
            return;
        }

        const table = el('table', 'data-table');
        const head = el('thead');
        const headRow = el('tr');
        [t('apps.column.name'), t('apps.column.package'), t('apps.column.version'),
         t('apps.column.state')].forEach((label) => headRow.appendChild(el('th', null, label)));
        head.appendChild(headRow);
        table.appendChild(head);

        const body = el('tbody');
        rows.forEach((app) => {
            const tr = el('tr');
            const name = el('td');
            name.appendChild(el('span', null, app.label));
            // The badge is on the NAME rather than in a column of its own: it is a property of
            // the app, and a fifth column for a flag that is true on four rows out of five in
            // the unfiltered view would be mostly whitespace.
            if (app.is_system) {
                name.appendChild(document.createTextNode(' '));
                name.appendChild(el('span', 'stat-card__meta', t('apps.system')));
            }
            tr.appendChild(name);
            const pkg = el('td', null, app.package);
            pkg.style.fontFamily = 'var(--font-mono)';
            pkg.style.wordBreak = 'break-all';
            tr.appendChild(pkg);
            tr.appendChild(el('td', null, app.version || ''));
            tr.appendChild(el('td', null, stateLabel(app)));
            body.appendChild(tr);
        });
        table.appendChild(body);
        bodyEl.appendChild(table);
    }

    function render(data) {
        // `reported_at: null` is "we have not been told", which is every Windows PC and every
        // Android device on an older agent. Distinct from a device that reported nothing --
        // which does not happen -- so the fold is absent rather than empty.
        fold.hidden = data.reported_at === null;
        if (fold.hidden) return;

        inventory = data;
        statusEl.textContent = t('apps.reported', {
            when: new Date(data.reported_at * 1000).toLocaleString(),
        });
        draw();
    }

    async function load() {
        let response;
        try {
            response = await fetch(`/api/apps/machines/${encodeURIComponent(machine)}`);
        } catch (e) {
            return;
        }
        if (!response.ok) {
            fold.hidden = true;      // out of scope, which for this operator is "no such card"
            return;
        }
        loaded = true;
        render(await response.json());
    }

    filterEl.addEventListener('input', draw);
    systemEl.addEventListener('change', draw);

    // Fetched when the fold opens, not on page load: a machine page that is mostly charts
    // should not spend a request on a card nobody has looked at, and this one can be four
    // hundred rows.
    fold.addEventListener('toggle', () => {
        if (fold.open && !loaded) load();
    });

    // ...but the fold must be REVEALED without being opened, or a device that can answer would
    // show no card at all until somebody clicked something they cannot see. One request on page
    // load decides that; it is the same request, so opening the fold afterwards costs nothing.
    load();
})();
