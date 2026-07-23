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
    metrics: document.getElementById('tab-metrics'),
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
    reset.dataset.role = 'reset';   // so an in-place save can toggle its visibility
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
    if (field.type === 'bool') {
        // A bool with a concrete default is a plain on/off toggle -> checkbox. Only a bool
        // whose default is null needs the third "unset -> follow .env" state, so hub.auto_update
        // keeps the tri-state select; the metrics.* collection toggles get a real checkbox.
        return (field.default === null || field.default === undefined)
            ? buildTriStateControl(field)
            : buildCheckboxControl(field);
    }
    if (field.type === 'str_list') return buildPreferenceControl(field);
    if (field.type === 'enum') return buildEnumControl(field);
    return buildNumberControl(field);
}

function buildCheckboxControl(field) {
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.className = 'checkbox';
    input.id = controlId(field.key);
    // field.value is the effective value (override if set, else the default), never null
    // for a concrete-default bool.
    input.checked = Boolean(field.value);
    input.addEventListener('change', () => { markDirty(field.key, input.checked); requestSave(); });
    return input;
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
    // input keeps the dirty state live (so Reset appears as you type); the save waits for
    // `change` -- i.e. blur or Enter -- so a half-typed number like "1" on the way to "120"
    // is not sent and rejected against its minimum mid-keystroke.
    input.addEventListener('input', () => {
        const raw = input.value.trim();
        markDirty(field.key, raw === '' ? null : Number(raw));
    });
    input.addEventListener('change', requestSave);
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
        requestSave();
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
    select.addEventListener('change', () => { markDirty(field.key, select.value); requestSave(); });
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
        requestSave();
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
}

function same(a, b) {
    if (Array.isArray(a) && Array.isArray(b)) {
        return a.length === b.length && a.every((v, i) => v === b[i]);
    }
    return a === b;
}

// The old Save/Discard bar is gone: every control saves itself the moment it is committed
// (see requestSave below). All that is left is a status line that shows "Saving…", then
// "Saved" or an error.
function renderActions(section) {
    const bar = document.createElement('div');
    bar.className = 'card-actions';
    bar.dataset.section = section.name;

    const status = document.createElement('span');
    status.className = 'autosave';
    status.dataset.role = 'autosave-status';
    bar.appendChild(status);

    return bar;
}

function setStatus(text, cls) {
    document.querySelectorAll('[data-role="autosave-status"]').forEach((node) => {
        node.textContent = text;
        node.className = cls ? `autosave ${cls}` : 'autosave';
    });
}

// --------------------------------------------------------------------------- saving

let saving = false;      // a POST is in flight
let pending = false;     // a control was committed while that POST was in flight

// Commit points (a select changed, a number field blurred, a preference reordered) call
// this rather than fetching directly. It serialises saves -- one request at a time -- and
// remembers if another edit landed mid-flight so nothing is dropped, WITHOUT auto-retrying
// a value the server just rejected (that would loop on a bad input).
function requestSave() {
    if (saving) { pending = true; return; }
    flushDirty();
}

async function flushDirty() {
    const keys = [...dirty.keys()];
    if (!keys.length) return;
    const updates = {};
    keys.forEach((key) => { updates[key] = dirty.get(key); });

    saving = true;
    pending = false;
    setStatus('Saving…', '');
    try {
        const resp = await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ updates }),
        });
        const body = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            // A 400 means the server rejected a value and wrote nothing. Its message names
            // the field, so show it inline against that control; the value stays dirty so
            // the next commit retries it -- but we do NOT auto-retry here, or a permanently
            // invalid entry would spin.
            showValidationError(body.error || `HTTP ${resp.status}`, updates);
            setStatus(body.error || `HTTP ${resp.status}`, 'autosave--error');
            return;
        }
        adoptSaved(body.settings, keys);
        setStatus('Saved', 'autosave--saved');
    } catch (e) {
        // Transport failure, not validation. Leave the value dirty and let the operator
        // re-commit; a background retry loop on a dropped network is worse than silence.
        setStatus(`Could not save: ${e.message}`, 'autosave--error');
    } finally {
        saving = false;
        if (pending) flushDirty();     // a genuine new edit arrived mid-request
    }
}

// Adopt the server's post-save view for JUST the keys we wrote, instead of rebuilding the
// whole panel the way an explicit Save used to. Rebuilding on every auto-save would yank
// focus out of the control being edited and, worse, discard a change the operator made
// while the request was in flight. So we update the saved baseline and the Reset button
// in place and leave every live control alone.
function adoptSaved(doc, savedKeys) {
    const map = new Map();
    for (const section of doc.sections) {
        for (const field of section.fields) map.set(field.key, field);
    }
    savedKeys.forEach((key) => {
        const field = map.get(key);
        if (!field) { dirty.delete(key); return; }
        fields.set(key, field);
        // If the control still shows exactly what we saved, it is no longer pending. If the
        // operator changed it again mid-request it stays dirty and requestSave's pending
        // pass will write it.
        if (dirty.has(key) && same(dirty.get(key), field.value)) dirty.delete(key);
        updateResetVisibility(key);
    });
}

function updateResetVisibility(key) {
    const field = fields.get(key);
    if (!field) return;
    const row = document.querySelector(`.setting[data-key="${key}"]`);
    if (!row) return;
    const reset = row.querySelector('[data-role="reset"]');
    if (reset) reset.hidden = field.is_default;
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
