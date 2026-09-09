// The machine page's Usage fold (roadmap #23 phase E): where this device's day went.
//
// **This is the one card on the page that is about a person rather than a machine.** Everything
// else here -- temperature, disks, processes, installed apps -- describes hardware or
// configuration. Foreground time describes what somebody did with their evening, and the fold
// is written accordingly: closed until opened, no polling, no fleet comparison, and a retention
// line stating in words how long the hub keeps it.
//
// **"No usage" and "not allowed to look" are rendered differently, and that is the point.**
// Usage access on Android is an appop that no Device Owner can grant; somebody has to walk over
// to the device and turn it on. A device in that state reports nothing, which is
// indistinguishable from a device nobody touched -- unless the console says so. The capability
// report (`usage_access`) is what lets it, and it is why this fold asks for it.
//
// Like the Apps and Location folds, the card is hidden entirely on a device that has never
// reported -- which is every Windows PC.
(function () {
    'use strict';

    const fold = document.getElementById('card-usage');
    if (!fold || !window.MachineContext || !window.MachineCapabilities) return;

    const machine = window.MachineContext.current();
    if (!machine) return;

    const bodyEl = document.getElementById('usage-body');
    const countEl = document.getElementById('usage-count');
    const statusEl = document.getElementById('usage-status');

    let ledger = null;

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    /** Seconds as hours and minutes. Never as a decimal: "1.4 h" is a number somebody has to
     *  convert before it means anything, and this column is read, not computed with. */
    function spell(seconds) {
        const total = Math.max(0, Math.round(Number(seconds) || 0));
        const hours = Math.floor(total / 3600);
        const minutes = Math.round((total % 3600) / 60);
        if (hours && minutes) return t('usage.hours_minutes', { hours: hours, minutes: minutes });
        if (hours) return t('usage.hours', { hours: hours });
        return t('usage.minutes', { minutes: minutes });
    }

    function totalsTable() {
        const table = el('table', 'data-table');
        const head = el('thead');
        const headRow = el('tr');
        [t('usage.column.app'), t('usage.column.time')].forEach((label) =>
            headRow.appendChild(el('th', null, label)));
        head.appendChild(headRow);
        table.appendChild(head);

        const body = el('tbody');
        // Twenty rows. A device reports a couple of hundred packages a week, most of them with
        // a few seconds of a system service, and the tail answers nothing anybody came here to
        // ask.
        ledger.totals.slice(0, 20).forEach((entry) => {
            const row = el('tr');
            const pkg = el('td', null, entry.package);
            pkg.style.fontFamily = 'var(--font-mono)';
            pkg.style.wordBreak = 'break-all';
            row.appendChild(pkg);
            row.appendChild(el('td', null, spell(entry.seconds)));
            body.appendChild(row);
        });
        table.appendChild(body);
        return table;
    }

    function daysTable() {
        const table = el('table', 'data-table');
        const head = el('thead');
        const headRow = el('tr');
        [t('usage.column.day'), t('usage.column.total'), t('usage.column.top')].forEach((label) =>
            headRow.appendChild(el('th', null, label)));
        head.appendChild(headRow);
        table.appendChild(head);

        const body = el('tbody');
        ledger.days.forEach((day) => {
            const row = el('tr');
            row.appendChild(el('td', null, day.day));
            row.appendChild(el('td', null, spell(day.total)));
            // Three, with their times, rather than a list of package names: "5 apps" says
            // nothing and twenty names say too much.
            const top = day.packages.slice(0, 3)
                .map((p) => `${p.package} ${spell(p.seconds)}`).join(', ');
            row.appendChild(el('td', null, top));
            body.appendChild(row);
        });
        table.appendChild(body);
        return table;
    }

    function draw() {
        bodyEl.replaceChildren();
        countEl.textContent = t('usage.days_counted', { count: ledger.days.length });

        if (ledger.days.length === 0) {
            bodyEl.appendChild(el('p', 'stat-card__meta', t('usage.none')));
            return;
        }
        bodyEl.appendChild(el('h3', 'section-title', t('usage.recent_total')));
        bodyEl.appendChild(totalsTable());
        bodyEl.appendChild(el('h3', 'section-title', t('usage.by_day')));
        bodyEl.appendChild(daysTable());
    }

    /** The line above the tables: how long this is kept, and -- when it applies -- that the
     *  device has not been allowed to measure it. */
    function status() {
        const lines = [t('usage.retention', { days: ledger.retention_days })];
        // Only said on a device that claims the schedule feature at all. On a Windows PC the
        // sentence would be true and meaningless.
        if (window.MachineCapabilities.hasFeature('time_policy')
            && !window.MachineCapabilities.hasFeature('usage_access')) {
            lines.push(t('usage.no_access'));
        }
        statusEl.textContent = lines.join(' ');
    }

    async function load() {
        let response;
        try {
            response = await fetch(`/api/usage/machines/${encodeURIComponent(machine)}`);
        } catch (e) {
            return;
        }
        if (!response.ok) {
            fold.hidden = true;      // out of scope, which for this operator is "no such card"
            return;
        }
        ledger = await response.json();

        await window.MachineCapabilities.ready();
        // Shown when the device has ever reported usage, OR when it is a device that would if
        // it could. The second case is the whole reason the fold can be empty: an operator has
        // to be able to see that a budget on this phone will never fire.
        const relevant = ledger.reported_at !== null
            || window.MachineCapabilities.hasFeature('time_policy');
        fold.hidden = !relevant;
        if (fold.hidden) return;

        status();
        draw();
    }

    load();
})();
