// Settings: operator-tunable hub and fleet configuration.
//
// Entirely schema-driven. GET /api/settings returns sections of field descriptors
// (type, bounds, unit, help, current value, default) and every control on the page is
// built from that -- so adding a knob is one line in settings.py's REGISTRY and nothing
// here changes. Resist hand-writing a field: the moment one knob is special-cased, the
// next person adds theirs the same way and the registry stops being the source of truth.
//
// Follows alerts.js/inventory.js conventions: build DOM with textContent, never
// innerHTML from data. That is load-bearing here rather than theoretical -- the sensor
// preference list contains sensor NAMES reported by agents, and /api/report is
// unauthenticated, so those strings are attacker-influenced.

const panels = {
    computer: document.getElementById('tab-computer'),
    hub: document.getElementById('tab-hub'),
    data: document.getElementById('tab-data'),
    fleet: document.getElementById('tab-fleet'),
};

// key -> pending value. An entry exists only while the control differs from what the
// server last confirmed, so nudging a number up and back leaves no phantom dirty state.
const dirty = new Map();
// key -> field descriptor from the last successful load; the saved baseline.
let fields = new Map();

// Guard against losing edits to a stray click on the sidebar. Registered once.
window.addEventListener('beforeunload', (e) => {
    if (dirty.size === 0) return;
    e.preventDefault();
    e.returnValue = '';
});

// --------------------------------------------------------------------------- loading

async function loadSettings() {
    const resp = await fetch('/api/settings');
    if (!resp.ok) return;
    const doc = await resp.json();
    applySchema(doc);
}

function applySchema(doc) {
    dirty.clear();
    fields = new Map();
    for (const section of doc.sections) {
        for (const field of section.fields) fields.set(field.key, field);
        const panel = panels[section.name];
        if (!panel) continue;      // a new section needs a panel div in settings.html
        panel.replaceChildren(renderSection(section));
    }
}

function renderSection(section) {
    const wrapper = document.createElement('div');

    const card = document.createElement('div');
    card.className = 'card';
    for (const field of section.fields) card.appendChild(renderField(field));
    wrapper.appendChild(card);

    wrapper.appendChild(renderActions(section));
    return wrapper;
}

// --------------------------------------------------------------------------- fields

function renderField(field) {
    const row = document.createElement('div');
    row.className = 'setting';
    row.dataset.key = field.key;

    const top = document.createElement('div');
    top.className = 'setting__row';

    const label = document.createElement('label');
    label.className = 'setting__label';
    label.textContent = field.label;
    label.htmlFor = controlId(field.key);
    top.appendChild(label);

    const control = buildControl(field);
    top.appendChild(control);

    if (field.unit && field.type !== 'bool' && field.type !== 'str_list') {
        const unit = document.createElement('span');
        unit.className = 'setting__unit';
        unit.textContent = field.unit;
        top.appendChild(unit);
    }

    const reset = document.createElement('button');
    reset.type = 'button';
    reset.className = 'btn btn--ghost';
    reset.textContent = 'Reset';
    // Only offered when there is something to reset -- a Reset next to an untouched
    // field is a button that does nothing.
    reset.hidden = field.is_default;
    reset.addEventListener('click', () => resetField(field.key, reset));
    top.appendChild(reset);

    row.appendChild(top);

    if (field.help) {
        const help = document.createElement('p');
        help.className = 'setting__help';
        help.textContent = field.help;
        row.appendChild(help);
    }

    const dflt = document.createElement('div');
    dflt.className = 'setting__default';
    dflt.textContent = `Default: ${describe(field, field.default)}`;
    row.appendChild(dflt);

    const error = document.createElement('div');
    error.className = 'setting__error';
    error.id = errorId(field.key);
    row.appendChild(error);

    return row;
}

function buildControl(field) {
    if (field.type === 'bool') return buildTriStateControl(field);
    if (field.type === 'str_list') return buildPreferenceControl(field);
    if (field.type === 'enum') return buildEnumControl(field);
    return buildNumberControl(field);
}

function buildNumberControl(field) {
    const input = document.createElement('input');
    input.className = 'input setting__input';
    input.id = controlId(field.key);
    input.type = 'number';
    if (field.min !== null && field.min !== undefined) input.min = String(field.min);
    if (field.max !== null && field.max !== undefined) input.max = String(field.max);
    if (field.type === 'float') input.step = 'any';
    input.value = field.value === null || field.value === undefined ? '' : String(field.value);
    input.addEventListener('input', () => {
        const raw = input.value.trim();
        markDirty(field.key, raw === '' ? null : Number(raw));
    });
    return input;
}

// Tri-state, because hub.auto_update distinguishes "unset -> follow .env" from an
// explicit on/off. A checkbox cannot express three states, so this is a select.
function buildTriStateControl(field) {
    const select = document.createElement('select');
    select.className = 'select';
    select.id = controlId(field.key);
    for (const [value, text] of [['', 'Use .env default'], ['true', 'On'], ['false', 'Off']]) {
        const opt = document.createElement('option');
        opt.value = value;
        opt.textContent = text;
        select.appendChild(opt);
    }
    select.value = field.value === null || field.value === undefined ? '' : String(field.value);
    select.addEventListener('change', () => {
        markDirty(field.key, select.value === '' ? null : select.value === 'true');
    });
    return select;
}

function buildEnumControl(field) {
    const select = document.createElement('select');
    select.className = 'select';
    select.id = controlId(field.key);
    for (const choice of field.choices || []) {
        const opt = document.createElement('option');
        opt.value = choice;
        opt.textContent = choice;
        select.appendChild(opt);
    }
    select.value = field.value == null ? '' : String(field.value);
    select.addEventListener('change', () => markDirty(field.key, select.value));
    return select;
}

// Ordered preference list: order IS the setting, so the control has to express rank,
// not just membership. Reorder/remove, plus an add picker seeded from sensor names the
// fleet is actually reporting (field.choices), falling back to free text so an operator
// can still enter a name for a machine that is currently offline.
function buildPreferenceControl(field) {
    const box = document.createElement('div');
    box.style.flexBasis = '100%';
    box.id = controlId(field.key);

    let items = Array.isArray(field.value) ? field.value.slice() : [];

    const list = document.createElement('ol');
    list.className = 'pref-list';

    const redraw = () => {
        list.replaceChildren();
        items.forEach((name, index) => {
            const li = document.createElement('li');
            li.className = 'pref-list__item';

            const rank = document.createElement('span');
            rank.className = 'pref-list__rank';
            rank.textContent = `${index + 1}.`;
            li.appendChild(rank);

            const text = document.createElement('span');
            text.className = 'pref-list__name';
            text.textContent = name;          // agent-supplied; never innerHTML
            li.appendChild(text);

            li.appendChild(moveButton('↑', 'Move up', index > 0, () => {
                [items[index - 1], items[index]] = [items[index], items[index - 1]];
                commit();
            }));
            li.appendChild(moveButton('↓', 'Move down', index < items.length - 1, () => {
                [items[index + 1], items[index]] = [items[index], items[index + 1]];
                commit();
            }));
            li.appendChild(moveButton('✕', 'Remove', items.length > 1, () => {
                items.splice(index, 1);
                commit();
            }));

            list.appendChild(li);
        });
    };

    const commit = () => {
        redraw();
        markDirty(field.key, items.slice());
    };

    box.appendChild(list);

    const adder = document.createElement('div');
    adder.className = 'toolbar';
    adder.style.marginBottom = '0';

    const picker = document.createElement('select');
    picker.className = 'select';
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = (field.choices && field.choices.length)
        ? 'Add a sensor…'
        : 'No sensors reported yet';
    picker.appendChild(placeholder);
    for (const choice of field.choices || []) {
        const opt = document.createElement('option');
        opt.value = choice;
        opt.textContent = choice;             // agent-supplied; never innerHTML
        picker.appendChild(opt);
    }
    adder.appendChild(picker);

    const custom = document.createElement('input');
    custom.className = 'input';
    custom.type = 'text';
    custom.placeholder = 'or type a sensor name';
    adder.appendChild(custom);

    const add = document.createElement('button');
    add.type = 'button';
    add.className = 'btn btn--ghost';
    add.textContent = 'Add';
    add.addEventListener('click', () => {
        const name = (custom.value.trim() || picker.value).toLowerCase();
        if (!name || items.includes(name)) return;
        items.push(name);
        custom.value = '';
        picker.value = '';
        commit();
    });
    adder.appendChild(add);

    box.appendChild(adder);
    redraw();
    return box;
}

function moveButton(glyph, title, enabled, onClick) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn--ghost';
    btn.textContent = glyph;
    btn.title = title;
    btn.setAttribute('aria-label', title);
    btn.disabled = !enabled;
    btn.addEventListener('click', onClick);
    return btn;
}

// --------------------------------------------------------------------------- dirty state

function markDirty(key, value) {
    const field = fields.get(key);
    if (field && same(field.value, value)) {
        dirty.delete(key);       // back to the saved value; no longer a pending change
    } else {
        dirty.set(key, value);
    }
    clearError(key);
    refreshActions();
}

function same(a, b) {
    if (Array.isArray(a) && Array.isArray(b)) {
        return a.length === b.length && a.every((v, i) => v === b[i]);
    }
    return a === b;
}

function renderActions(section) {
    const bar = document.createElement('div');
    bar.className = 'settings-actions';
    bar.dataset.section = section.name;
    bar.hidden = true;

    const status = document.createElement('span');
    status.className = 'settings-actions__status';
    bar.appendChild(status);

    const discard = document.createElement('button');
    discard.type = 'button';
    discard.className = 'btn btn--ghost';
    discard.textContent = 'Discard';
    discard.addEventListener('click', () => loadSettings());
    bar.appendChild(discard);

    const save = document.createElement('button');
    save.type = 'button';
    save.className = 'btn btn--primary';
    save.textContent = 'Save changes';
    save.addEventListener('click', () => saveSection(section.name, save));
    bar.appendChild(save);

    return bar;
}

function refreshActions() {
    for (const [name, panel] of Object.entries(panels)) {
        if (!panel) continue;
        const bar = panel.querySelector('.settings-actions');
        if (!bar) continue;
        const count = keysInPanel(panel).filter((k) => dirty.has(k)).length;
        bar.hidden = count === 0;
        const status = bar.querySelector('.settings-actions__status');
        if (status) {
            status.textContent = count === 1 ? '1 unsaved change' : `${count} unsaved changes`;
        }
    }
}

function keysInPanel(panel) {
    return Array.from(panel.querySelectorAll('.setting')).map((el) => el.dataset.key);
}

// --------------------------------------------------------------------------- saving

async function saveSection(sectionName, btn) {
    const panel = panels[sectionName];
    if (!panel) return;

    const updates = {};
    for (const key of keysInPanel(panel)) {
        if (dirty.has(key)) updates[key] = dirty.get(key);
    }
    if (Object.keys(updates).length === 0) return;
    if (!confirmDestructive(updates)) return;

    btn.disabled = true;
    btn.textContent = 'Saving…';
    try {
        const resp = await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ updates }),
        });
        const body = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            // A 400 means the server rejected a value and wrote nothing. Its message
            // names the field, so show it inline rather than in an alert box -- a
            // settings form is exactly where inline errors earn their keep.
            showValidationError(body.error || `HTTP ${resp.status}`, updates);
            return;
        }
        applySchema(body.settings);
        refreshActions();
    } catch (e) {
        // Transport failure, not validation -- matches alerts.js.
        window.alert(`Could not save settings: ${e.message}`);
    } finally {
        btn.disabled = false;
        btn.textContent = 'Save changes';
    }
}

// Retention is the one knob whose save destroys data, and it does so later, in a
// background thread. Without this the operator finds out from a graph that has silently
// gone empty.
function confirmDestructive(updates) {
    const key = 'data.retention_days';
    if (!(key in updates)) return true;
    const field = fields.get(key);
    const next = Number(updates[key]);
    if (!field || !Number.isFinite(next) || next >= Number(field.value)) return true;
    return window.confirm(
        `Shorten retention from ${field.value} to ${next} days?\n\n` +
        `Readings older than ${next} days will be PERMANENTLY DELETED on the next prune. ` +
        `This cannot be undone.`);
}

function showValidationError(message, updates) {
    // The message is prefixed with the field's label; find whose it is so it can be
    // shown against the right input. Falls back to the first edited field.
    const keys = Object.keys(updates);
    const match = keys.find((key) => {
        const field = fields.get(key);
        return field && message.startsWith(field.label);
    });
    const target = match || keys[0];
    const slot = document.getElementById(errorId(target));
    if (slot) slot.textContent = message;
    else window.alert(message);
}

function clearError(key) {
    const slot = document.getElementById(errorId(key));
    if (slot) slot.textContent = '';
}

async function resetField(key, btn) {
    btn.disabled = true;
    try {
        const resp = await fetch('/api/settings/reset', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ keys: [key] }),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const body = await resp.json();
        applySchema(body.settings);
        refreshActions();
    } catch (e) {
        btn.disabled = false;
        window.alert(`Could not reset: ${e.message}`);
    }
}

// --------------------------------------------------------------------------- helpers

function controlId(key) { return `set-${key.replace(/\./g, '-')}`; }
function errorId(key) { return `err-${key.replace(/\./g, '-')}`; }

function describe(field, value) {
    if (value === null || value === undefined) return 'follow .env';
    if (Array.isArray(value)) return value.join(' → ');
    if (typeof value === 'boolean') return value ? 'on' : 'off';
    return field.unit ? `${value} ${field.unit}` : String(value);
}

loadSettings();
