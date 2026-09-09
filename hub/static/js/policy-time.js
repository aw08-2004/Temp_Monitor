// Schedules (roadmap #23 phase E): when a managed device may run what.
//
// **A separate module from policy.js, on the same page.** The two share a machine picker and
// nothing else: one composes a package list, the other composes hours and minutes, and merging
// them would produce a single editor that hides most of itself depending on a mode field. The
// duplication that buys is one picker, forty lines, and it is the cheaper half of the trade.
//
// **A week starts on MONDAY, and the wire format is an index, never a name.** The agent reads
// day 0 as Monday (DeviceSchedule.DayIndex); a locale-dependent first day here would put a
// Sunday curfew on a Saturday for half the fleet, silently, and only for the people whose
// browser was set differently from whoever wrote the policy.
//
// **A window whose end is before its start wraps past midnight, and the editor says so rather
// than refusing.** 22:00 to 07:00 is the only kind of curfew anybody actually writes.
//
// No preview, unlike the blocklist above it: see the note in policy.html.
(function () {
    'use strict';

    const listEl = document.getElementById('time-list');
    if (!listEl) return;

    const editor = document.getElementById('time-editor');
    const newButton = document.getElementById('time-new');
    const nameEl = document.getElementById('time-name');
    const enabledEl = document.getElementById('time-enabled');
    const fleetEl = document.getElementById('time-fleet');
    const machinesWrap = document.getElementById('time-machines-wrap');
    const machinesEl = document.getElementById('time-machines');
    const machineFilterEl = document.getElementById('time-machine-filter');
    const windowsEl = document.getElementById('time-windows');
    const budgetsEl = document.getElementById('time-budgets');
    const addWindowButton = document.getElementById('time-add-window');
    const addBudgetButton = document.getElementById('time-add-budget');
    const saveButton = document.getElementById('time-save');
    const cancelButton = document.getElementById('time-cancel');
    const errorEl = document.getElementById('time-error');

    let policies = [];
    let packages = [];
    let machines = [];
    let canManage = false;
    let everyPackage = '*';
    let editing = null;

    // The draft's rules, held as objects rather than read back out of the DOM: a row that is
    // removed while another is being edited would otherwise renumber everything after it.
    let windows = [];
    let budgets = [];
    const chosenMachines = new Set();

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    /** Monday first, matching the wire format. Seven literal keys rather than one built by
     *  appending the day number, because tests/test_i18n.py only scans literals -- a computed
     *  key is invisible to the test that would have caught a missing translation. */
    function dayNames() {
        return [t('policy.time.day.mon'), t('policy.time.day.tue'), t('policy.time.day.wed'),
                t('policy.time.day.thu'), t('policy.time.day.fri'), t('policy.time.day.sat'),
                t('policy.time.day.sun')];
    }

    /** Minutes past midnight as "HH:MM", which is what an <input type="time"> wants and what
     *  policy.py's _minute_of_day accepts back. */
    function clock(minutes) {
        const value = Math.max(0, Math.min(1439, Number(minutes) || 0));
        const hours = String(Math.floor(value / 60)).padStart(2, '0');
        return `${hours}:${String(value % 60).padStart(2, '0')}`;
    }

    function packageName(name) {
        if (name === everyPackage) return t('policy.time.whole_device');
        const found = packages.find((p) => p.package === name);
        return found ? found.label : name;
    }

    // ---------------------------------------------------------------- the list
    function describeWindow(entry) {
        const names = dayNames();
        const days = (entry.days || []).map((d) => names[d] || d).join(' ');
        const apps = (entry.packages || []).map(packageName).join(', ');
        return t('policy.time.window_summary', {
            days: days, start: clock(entry.start), end: clock(entry.end), apps: apps,
        });
    }

    function renderList() {
        listEl.replaceChildren();
        if (policies.length === 0) {
            listEl.appendChild(el('p', 'stat-card__meta', t('policy.time.none')));
            return;
        }
        const table = el('table', 'data-table');
        const head = el('thead');
        const headRow = el('tr');
        [t('policy.column.name'), t('policy.time.column.rules'), t('policy.column.targets'), '']
            .forEach((label) => headRow.appendChild(el('th', null, label)));
        head.appendChild(headRow);
        table.appendChild(head);

        const body = el('tbody');
        policies.forEach((entry) => {
            const row = el('tr');
            const name = el('td');
            name.appendChild(el('span', null, entry.name));
            if (!entry.enabled) {
                name.appendChild(document.createTextNode(' '));
                name.appendChild(el('span', 'stat-card__meta', t('policy.off')));
            }
            row.appendChild(name);

            // Spelled out rather than counted. "2 rules" is a number nobody can check against
            // what they meant; "Mon Tue 22:00-07:00, whole device" is the policy itself.
            const rules = el('td');
            (entry.windows || []).forEach((w) =>
                rules.appendChild(el('div', null, describeWindow(w))));
            (entry.budgets || []).forEach((b) =>
                rules.appendChild(el('div', null, t('policy.time.budget_summary', {
                    app: packageName(b.package), minutes: b.minutes,
                }))));
            row.appendChild(rules);

            const targets = el('td', null, entry.fleet_wide
                ? t('policy.targets.fleet')
                : t('policy.targets.machines', { count: entry.machines.length }));
            if (entry.machines_hidden) {
                targets.appendChild(document.createTextNode(' '));
                targets.appendChild(el('span', 'stat-card__meta',
                    t('policy.targets.hidden', { count: entry.machines_hidden })));
            }
            row.appendChild(targets);

            const actions = el('td');
            if (canManage) {
                const edit = el('button', 'btn btn--ghost', t('policy.edit'));
                edit.type = 'button';
                edit.addEventListener('click', () => openEditor(entry));
                actions.appendChild(edit);
                const remove = el('button', 'btn btn--ghost', t('policy.delete'));
                remove.type = 'button';
                remove.addEventListener('click', () => destroy(entry));
                actions.appendChild(remove);
            }
            row.appendChild(actions);
            body.appendChild(row);
        });
        table.appendChild(body);
        listEl.appendChild(table);
    }

    // ---------------------------------------------------------------- rule rows
    /** The package chooser shared by both rule kinds. "Whole device" is the FIRST option and
     *  the default, because it is what most rules mean and because a rule that names no
     *  package would otherwise be unexpressible. */
    function packageSelect(selected, multiple) {
        const select = document.createElement('select');
        select.className = 'select';
        select.multiple = Boolean(multiple);
        if (multiple) select.size = 5;

        const whole = document.createElement('option');
        whole.value = everyPackage;
        whole.textContent = t('policy.time.whole_device');
        select.appendChild(whole);

        packages.forEach((p) => {
            const option = document.createElement('option');
            option.value = p.package;
            option.textContent = `${p.label} (${p.package})`;
            select.appendChild(option);
        });

        const chosen = new Set(selected || []);
        Array.from(select.options).forEach((option) => {
            option.selected = chosen.has(option.value);
        });
        if (![...select.options].some((o) => o.selected)) select.options[0].selected = true;
        return select;
    }

    function renderWindows() {
        windowsEl.replaceChildren();
        if (windows.length === 0) {
            windowsEl.appendChild(el('p', 'stat-card__meta', t('policy.time.no_windows')));
        }
        windows.forEach((entry, index) => {
            const card = el('div', 'policy-rule');

            const days = el('div', 'toolbar');
            dayNames().forEach((label, day) => {
                const wrap = el('label', 'stat-card__meta');
                const box = document.createElement('input');
                box.type = 'checkbox';
                box.className = 'checkbox';
                box.checked = entry.days.includes(day);
                box.addEventListener('change', () => {
                    if (box.checked) entry.days = [...new Set([...entry.days, day])].sort();
                    else entry.days = entry.days.filter((d) => d !== day);
                });
                wrap.appendChild(box);
                wrap.appendChild(document.createTextNode(' ' + label));
                days.appendChild(wrap);
            });
            card.appendChild(days);

            const times = el('div', 'toolbar');
            const start = document.createElement('input');
            start.type = 'time';
            start.className = 'input';
            start.value = clock(entry.start);
            start.setAttribute('aria-label', t('policy.time.from'));
            start.addEventListener('change', () => { entry.start = start.value; wrapNote(); });
            const end = document.createElement('input');
            end.type = 'time';
            end.className = 'input';
            end.value = clock(entry.end);
            end.setAttribute('aria-label', t('policy.time.until'));
            end.addEventListener('change', () => { entry.end = end.value; wrapNote(); });

            times.appendChild(el('span', 'stat-card__meta', t('policy.time.from')));
            times.appendChild(start);
            times.appendChild(el('span', 'stat-card__meta', t('policy.time.until')));
            times.appendChild(end);
            const note = el('span', 'stat-card__meta');
            times.appendChild(note);
            card.appendChild(times);

            // Said out loud rather than left to be discovered: an end before a start is not a
            // mistake here, it is the ordinary bedtime rule, and an editor that stayed silent
            // about it would read as one that had accepted nonsense.
            function wrapNote() {
                const from = String(start.value || '');
                const until = String(end.value || '');
                note.textContent = (from && until && until < from)
                    ? t('policy.time.wraps') : '';
            }
            wrapNote();

            const apps = packageSelect(entry.packages, true);
            apps.addEventListener('change', () => {
                entry.packages = Array.from(apps.selectedOptions).map((o) => o.value);
            });
            card.appendChild(apps);

            const remove = el('button', 'btn btn--ghost', t('policy.time.remove_rule'));
            remove.type = 'button';
            remove.addEventListener('click', () => {
                windows.splice(index, 1);
                renderWindows();
            });
            card.appendChild(remove);
            windowsEl.appendChild(card);
        });
    }

    function renderBudgets() {
        budgetsEl.replaceChildren();
        if (budgets.length === 0) {
            budgetsEl.appendChild(el('p', 'stat-card__meta', t('policy.time.no_budgets')));
        }
        budgets.forEach((entry, index) => {
            const card = el('div', 'policy-rule toolbar');

            const app = packageSelect([entry.package], false);
            app.addEventListener('change', () => { entry.package = app.value; });
            card.appendChild(app);

            const minutes = document.createElement('input');
            minutes.type = 'number';
            minutes.className = 'input';
            minutes.min = '0';
            minutes.max = '1440';
            minutes.value = String(entry.minutes);
            minutes.style.maxWidth = '120px';
            minutes.setAttribute('aria-label', t('policy.time.minutes'));
            minutes.addEventListener('change', () => { entry.minutes = minutes.value; });
            card.appendChild(minutes);
            card.appendChild(el('span', 'stat-card__meta', t('policy.time.minutes_a_day')));

            const remove = el('button', 'btn btn--ghost', t('policy.time.remove_rule'));
            remove.type = 'button';
            remove.addEventListener('click', () => {
                budgets.splice(index, 1);
                renderBudgets();
            });
            card.appendChild(remove);
            budgetsEl.appendChild(card);
        });
    }

    function renderMachines() {
        machinesWrap.hidden = fleetEl.checked;
        const needle = (machineFilterEl.value || '').trim().toLowerCase();
        machinesEl.replaceChildren();
        machines.filter((m) => !needle || m.toLowerCase().includes(needle)).forEach((machine) => {
            const row = el('label', 'policy-picker__row');
            const box = document.createElement('input');
            box.type = 'checkbox';
            box.className = 'checkbox';
            box.checked = chosenMachines.has(machine);
            box.addEventListener('change', () => {
                if (box.checked) chosenMachines.add(machine);
                else chosenMachines.delete(machine);
            });
            row.appendChild(box);
            row.appendChild(el('span', 'policy-picker__label', machine));
            machinesEl.appendChild(row);
        });
    }

    // ---------------------------------------------------------------- the editor
    function openEditor(entry) {
        editing = entry || null;
        nameEl.value = entry ? entry.name : '';
        enabledEl.checked = entry ? entry.enabled : true;
        fleetEl.checked = entry ? entry.fleet_wide : false;
        // Copied, not referenced: an operator who edits and then cancels must not have changed
        // the row still showing in the list behind the editor.
        windows = (entry ? entry.windows : []).map((w) => ({
            days: [...(w.days || [])], start: w.start, end: w.end,
            packages: [...(w.packages || [])],
        }));
        budgets = (entry ? entry.budgets : []).map((b) => ({
            package: b.package, minutes: b.minutes,
        }));
        chosenMachines.clear();
        (entry ? entry.machines : []).forEach((m) => chosenMachines.add(m));
        errorEl.textContent = '';
        renderWindows();
        renderBudgets();
        renderMachines();
        editor.hidden = false;
        nameEl.focus();
    }

    function draft() {
        return {
            name: nameEl.value,
            enabled: enabledEl.checked,
            fleet_wide: fleetEl.checked,
            machines: [...chosenMachines],
            // Sent as typed. "22:00" and a minute count are both accepted by policy.py, and
            // converting here would mean a second implementation of a parse that already
            // exists on the side that decides.
            windows: windows.map((w) => ({
                days: w.days, start: w.start, end: w.end, packages: w.packages,
            })),
            budgets: budgets.map((b) => ({ package: b.package, minutes: Number(b.minutes) })),
        };
    }

    async function save() {
        errorEl.textContent = '';
        const url = editing ? `/api/policy/times/${encodeURIComponent(editing.id)}`
                            : '/api/policy/times';
        let response;
        try {
            response = await fetch(url, {
                method: editing ? 'PUT' : 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(draft()),
            });
        } catch (e) {
            errorEl.textContent = t('policy.unreachable');
            return;
        }
        if (!response.ok) {
            const data = await response.json().catch(() => ({}));
            // Verbatim: policy.py names the specific refusal (a curfew that starts and ends at
            // the same minute, a budget on a package that can never be suspended), and a
            // generic message would throw away the only part that helps.
            errorEl.textContent = data.error || t('policy.failed');
            return;
        }
        editor.hidden = true;
        editing = null;
        await load();
    }

    async function destroy(entry) {
        if (!window.confirm(t('policy.confirm_delete', { name: entry.name }))) return;
        try {
            await fetch(`/api/policy/times/${encodeURIComponent(entry.id)}`,
                        { method: 'DELETE' });
        } catch (e) {
            return;
        }
        await load();
    }

    // ---------------------------------------------------------------- load
    async function load() {
        const [policyResp, packageResp, machineResp] = await Promise.all([
            fetch('/api/policy/times').catch(() => null),
            fetch('/api/apps/packages').catch(() => null),
            fetch('/api/machines').catch(() => null),
        ]);
        if (!policyResp || !policyResp.ok) return;

        const data = await policyResp.json();
        policies = data.policies || [];
        canManage = Boolean(data.can_manage);
        everyPackage = data.every_package || '*';
        newButton.hidden = !canManage;

        if (packageResp && packageResp.ok) {
            // Protected packages are left out of THIS picker, unlike the blocklist's. There a
            // disabled row explains why the launcher cannot be blocked; here the same row would
            // sit in a native <select> with no way to say so, and policy.py refuses a budget on
            // one anyway.
            const prefixes = (data.protected_prefixes || []).map((p) => p.toLowerCase());
            packages = ((await packageResp.json()).packages || []).filter((p) => {
                const name = (p.package || '').toLowerCase();
                return !prefixes.some((prefix) => name.startsWith(prefix));
            });
        }
        if (machineResp && machineResp.ok) {
            machines = ((await machineResp.json()) || []).map((m) => m.machine).sort();
        }
        renderList();
    }

    newButton.addEventListener('click', () => openEditor(null));
    cancelButton.addEventListener('click', () => { editor.hidden = true; editing = null; });
    saveButton.addEventListener('click', save);
    fleetEl.addEventListener('change', renderMachines);
    machineFilterEl.addEventListener('input', renderMachines);
    addWindowButton.addEventListener('click', () => {
        // 22:00 to 07:00, whole device: the rule somebody opening this section came to write.
        windows.push({ days: [0, 1, 2, 3, 4], start: 1320, end: 420, packages: [everyPackage] });
        renderWindows();
    });
    addBudgetButton.addEventListener('click', () => {
        budgets.push({ package: everyPackage, minutes: 120 });
        renderBudgets();
    });

    load();
})();
