// Rules page: write a condition over machine data, say which PCs it applies to, and say
// what happens when it matches.
//
// Same two house rules as packages.js and permissions.js, for the same reasons:
//
//  * Everything is built with textContent / createElement, never innerHTML. Variable
//    values, hostnames, OU paths and message bodies are all arbitrary strings that came
//    from operators or from agents.
//  * The vocabularies — variables, operators, action types, button presets — come from
//    GET /api/rules/variables and the catalog the server sends, not a copy here. A
//    hardcoded operator list silently stops offering a new one, which reads to an operator
//    as "the feature is broken".
//
// The builder and the expression box are two views of ONE condition. Switching between
// them round-trips through the server (POST /api/rules/preview returns the canonical AST
// and its text form), so the two can never drift into disagreeing about what a rule means.

const rulesBody = document.getElementById('rules-body');
const rulesEmpty = document.getElementById('rules-empty');
const rulesCount = document.getElementById('rules-count');
const actionsBanner = document.getElementById('rules-actions-banner');
const editor = document.getElementById('rule-editor');

let catalog = { variables: [], operators: {} };
let byName = new Map();
let canManage = false;
let canIssueCommands = false;
let commandActionsEnabled = false;
let editingId = null;
let mode = 'builder';

// The editor's working copy. Kept as plain data rather than read back out of the DOM, so
// that switching between the builder and the expression view never loses a half-typed
// clause.
let draft = null;
let commandByName = new Map();
let actionByName = new Map();
let scriptByName = new Map();

// The action types, the commands and their parameters, the button presets and the message
// outcomes all now arrive from GET /api/rules/variables. This file used to carry a hand-kept
// copy of every one of them and every copy had drifted -- seven of twelve commands, five of
// six action types, five of six presets -- which is exactly what the house rule at the top of
// this file exists to prevent. `catalog` holds them; there are no lists here any more.

async function api(path, options) {
    const resp = await fetch(path, options);
    let body = null;
    try { body = await resp.json(); } catch (e) { /* empty body is fine */ }
    if (!resp.ok) throw new Error((body && body.error) || `HTTP ${resp.status}`);
    return body;
}

function json(method, payload) {
    // Content-Type: application/json is load-bearing, not cosmetic — it is what makes a
    // cross-origin POST preflight and fail. See fleet_web.py's module docstring.
    return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) };
}

function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
}

function opt(value, label) {
    const node = document.createElement('option');
    node.value = value;
    node.textContent = label;
    return node;
}

function fmtTime(epoch) {
    if (!epoch) return '—';
    return new Date(epoch * 1000).toLocaleString();
}

// ---------------------------------------------------------------- catalog

let scriptCatalog = { scripts: [], can_edit: false, shells: ['powershell'], max_body_chars: 10000 };

async function loadCatalog() {
    catalog = await api('/api/rules/variables');
    byName = new Map(catalog.variables.map((v) => [v.name, v]));
    commandByName = new Map((catalog.commands || []).map((c) => [c.name, c]));
    actionByName = new Map((catalog.action_types || []).map((a) => [a.name, a]));
    scriptCatalog = await api('/api/rules/scripts');
    scriptByName = new Map((scriptCatalog.scripts || []).map((x) => [x.name, x]));
}

function commandSpec(name) { return commandByName.get(name) || null; }
function scriptSpec(name) { return scriptByName.get(name) || null; }

function variableLabel(name) {
    const entry = byName.get(name);
    return entry ? `${entry.label} (${name})` : name;
}

function operatorsFor(name) {
    const entry = byName.get(name);
    return entry ? entry.operators : ['=='];
}

function operatorLabel(op) {
    return catalog.operators[op] || op;
}

// ---------------------------------------------------------------- rule list

async function loadRules() {
    const data = await api('/api/rules');
    canManage = data.can_manage;
    canIssueCommands = data.can_issue_commands;
    commandActionsEnabled = data.command_actions_enabled;
    actionsBanner.hidden = data.actions_enabled;
    document.getElementById('rules-new').hidden = !canManage;

    rulesBody.textContent = '';
    rulesEmpty.hidden = data.rules.length > 0;
    rulesCount.textContent = data.rules.length ? `${data.rules.length}` : '';

    data.rules.forEach((rule) => {
        const row = document.createElement('tr');
        const nameCell = el('td');
        nameCell.appendChild(el('strong', null, rule.name));
        if (!rule.enabled) {
            nameCell.appendChild(document.createTextNode(' '));
            nameCell.appendChild(el('span', 'badge', t('rules.disabled')));
        }
        if (rule.blocked) {
            nameCell.appendChild(el('div', 'stat-card__meta', rule.blocked));
        }
        if (rule.description) {
            // Clamped to two lines in CSS; the full text goes on `title` so a long description
            // is shortened in the list rather than lost.
            const desc = el('div', 'stat-card__meta rules-table__description', rule.description);
            desc.title = rule.description;
            nameCell.appendChild(desc);
        }
        row.appendChild(nameCell);
        row.appendChild(el('td', 'rules-table__condition', rule.condition_text));
        row.appendChild(el('td', null, (rule.actions || [])
            .map((a) => t(`rules.action.${a.type}.label`)).join(', ')));
        row.appendChild(el('td', null, String(rule.matching || 0)));

        const tools = el('td');
        // The buttons go in a flex row rather than straight into the cell: appended bare they
        // are inline elements with no gap, and the actions column is the one the table squeezes
        // first, so Edit / Disable / Delete wrapped onto three lines and set the height of
        // every row in the list. See .rules-table in components.css for the other half.
        const toolRow = el('div', 'rules-table__tools');
        tools.appendChild(toolRow);
        if (canManage) {
            const edit = el('button', 'btn btn--ghost', t('rules.edit'));
            edit.type = 'button';
            edit.addEventListener('click', () => openEditor(rule));
            toolRow.appendChild(edit);

            const toggle = el('button', 'btn btn--ghost',
                rule.enabled ? t('rules.disabled') : t('rules.enabled'));
            toggle.type = 'button';
            toggle.addEventListener('click', async () => {
                await api(`/api/rules/${rule.id}/enabled`, json('PUT', { enabled: !rule.enabled }));
                loadRules();
            });
            toolRow.appendChild(toggle);

            const remove = el('button', 'btn btn--ghost', t('rules.delete'));
            remove.type = 'button';
            remove.addEventListener('click', async () => {
                if (!window.confirm(t('rules.confirm_delete'))) return;
                await api(`/api/rules/${rule.id}`, { method: 'DELETE' });
                loadRules();
            });
            toolRow.appendChild(remove);
        }
        row.appendChild(tools);
        rulesBody.appendChild(row);
    });
}

// ---------------------------------------------------------------- editor

function blankDraft() {
    return {
        name: '',
        description: '',
        enabled: true,
        target: { include: [{ kind: 'all' }], exclude: [] },
        clauses: [{ var: 'sys.uptime_days', cmp: '>', value: 7 }],
        join: 'and',
        condition_text: 'sys.uptime_days > 7',
        actions: [],
        fire_once_per_match: false,
        for_seconds: 0,
        cooldown_seconds: 3600,
        max_targets_per_tick: 25,
    };
}

// A stored condition is a tree; the builder shows a flat list of clauses joined by one
// connective. Anything more nested than that is editable only as text — and says so, rather
// than being silently flattened into something that means something else.
function draftFromCondition(condition) {
    if (!condition) return { clauses: [], join: 'and', flat: true };
    if (condition.var) return { clauses: [condition], join: 'and', flat: true };
    if (condition.op === 'and' || condition.op === 'or') {
        const flat = (condition.nodes || []).every((n) => !!n.var);
        return { clauses: flat ? condition.nodes : [], join: condition.op, flat };
    }
    return { clauses: [], join: 'and', flat: false };
}

function conditionFromDraft() {
    if (draft.clauses.length === 1) return draft.clauses[0];
    return { op: draft.join, nodes: draft.clauses };
}

function openEditor(rule) {
    editingId = rule ? rule.id : null;
    if (rule) {
        const shape = draftFromCondition(rule.condition);
        draft = {
            name: rule.name,
            description: rule.description || '',
            enabled: rule.enabled,
            target: rule.target || { include: [{ kind: 'all' }], exclude: [] },
            clauses: shape.clauses,
            join: shape.join,
            condition_text: rule.condition_text || '',
            actions: rule.actions || [],
            fire_once_per_match: rule.fire_once_per_match,
            for_seconds: rule.for_seconds,
            cooldown_seconds: rule.cooldown_seconds,
            max_targets_per_tick: rule.max_targets_per_tick,
        };
        // A rule whose condition the flat builder cannot represent opens in the text view,
        // where it CAN be represented. Silently showing an incomplete builder would let an
        // operator save away half their own condition.
        mode = shape.flat ? 'builder' : 'expression';
    } else {
        draft = blankDraft();
        mode = 'builder';
    }
    editor.hidden = false;
    document.getElementById('rule-name').value = draft.name;
    document.getElementById('rule-description').value = draft.description;
    document.getElementById('rule-enabled').checked = draft.enabled;
    document.getElementById('rule-fire-once').checked = !!draft.fire_once_per_match;
    document.getElementById('rule-for').value = draft.for_seconds;
    document.getElementById('rule-cooldown').value = draft.cooldown_seconds;
    document.getElementById('rule-max-targets').value = draft.max_targets_per_tick;
    document.getElementById('rule-join').value = draft.join;
    document.getElementById('rule-expression').value = draft.condition_text;
    document.getElementById('rule-save-error').textContent = '';
    document.getElementById('rule-preview-result').textContent = '';
    document.getElementById('rule-preview-rows').textContent = '';
    renderTargets();
    renderClauses();
    renderActions();
    setMode(mode);
    refreshTargetCount();
    editor.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function closeEditor() {
    editor.hidden = true;
    draft = null;
    editingId = null;
}

// ---------------------------------------------------------------- targets

function renderTargets() {
    ['include', 'exclude'].forEach((side) => {
        const host = document.getElementById(
            side === 'include' ? 'rule-target-include' : 'rule-target-exclude');
        host.textContent = '';
        draft.target[side] = draft.target[side] || [];
        draft.target[side].forEach((selector, index) => {
            host.appendChild(selectorRow(side, selector, index));
        });
    });
}

function selectorRow(side, selector, index) {
    const row = el('div', 'toolbar');
    row.style.marginBottom = 'var(--space-2)';
    row.appendChild(el('span', 'stat-card__meta', t(`rules.target.${selector.kind}`)));

    if (selector.kind === 'machines') {
        const input = el('input', 'input');
        input.type = 'text';
        input.value = (selector.machines || []).join(', ');
        input.placeholder = 'PC-1, PC-2';
        input.style.minWidth = '320px';
        input.addEventListener('change', () => {
            selector.machines = input.value.split(',').map((s) => s.trim()).filter(Boolean);
            refreshTargetCount();
        });
        row.appendChild(input);
    } else if (selector.kind === 'ad_ou') {
        const input = el('input', 'input');
        input.type = 'text';
        input.value = selector.ou || '';
        input.placeholder = 'OU=Sales,DC=corp';
        input.style.minWidth = '320px';
        input.addEventListener('change', () => { selector.ou = input.value.trim(); refreshTargetCount(); });
        row.appendChild(input);
        const label = el('label', 'stat-card__meta');
        const box = document.createElement('input');
        box.type = 'checkbox';
        box.checked = selector.include_children !== false;
        box.addEventListener('change', () => {
            selector.include_children = box.checked;
            refreshTargetCount();
        });
        label.appendChild(box);
        label.appendChild(document.createTextNode(' ' + t('rules.include_children')));
        row.appendChild(label);
    } else if (selector.kind === 'field') {
        const name = el('input', 'input');
        name.type = 'text';
        name.value = selector.field || '';
        name.placeholder = 'location';
        name.addEventListener('change', () => { selector.field = name.value.trim(); refreshTargetCount(); });
        row.appendChild(name);
        const value = el('input', 'input');
        value.type = 'text';
        value.value = selector.value === undefined ? '' : String(selector.value);
        value.addEventListener('change', () => { selector.value = value.value; refreshTargetCount(); });
        row.appendChild(value);
    }

    const remove = el('button', 'btn btn--ghost', '×');
    remove.type = 'button';
    remove.addEventListener('click', () => {
        draft.target[side].splice(index, 1);
        renderTargets();
        refreshTargetCount();
    });
    row.appendChild(remove);
    return row;
}

async function refreshTargetCount() {
    const label = document.getElementById('rule-target-count');
    try {
        const data = await api('/api/rules/targets', json('POST', { target: draft.target }));
        label.textContent = `${data.count} ${t('rules.targets')}`;
        if (data.machines.length) {
            label.title = data.machines.slice(0, 50).join(', ');
        }
    } catch (e) {
        label.textContent = e.message;
    }
}

// ---------------------------------------------------------------- condition

function setMode(next) {
    mode = next;
    const isBuilder = mode === 'builder';
    document.getElementById('rule-clauses').hidden = !isBuilder;
    document.getElementById('rule-builder-tools').hidden = !isBuilder;
    document.getElementById('rule-join').hidden = !isBuilder;
    document.getElementById('rule-expression').hidden = isBuilder;
}

function renderClauses() {
    const host = document.getElementById('rule-clauses');
    host.textContent = '';
    draft.clauses.forEach((clause, index) => host.appendChild(clauseRow(clause, index)));
}

function clauseRow(clause, index) {
    const row = el('div', 'toolbar');
    row.style.marginBottom = 'var(--space-2)';

    const variable = el('select', 'input');
    catalog.variables.forEach((v) => variable.appendChild(opt(v.name, variableLabel(v.name))));
    // A stored clause may name a per-volume variable (disk.d.free_gb) that the fleet-wide
    // catalog does not list, because drive letters are per-machine. Keep it selectable
    // rather than silently rewriting the operator's clause to whatever sorts first.
    if (!byName.has(clause.var)) variable.appendChild(opt(clause.var, clause.var));
    variable.value = clause.var;

    const operator = el('select', 'input');
    const rebuildOperators = () => {
        operator.textContent = '';
        operatorsFor(clause.var).forEach((op) => operator.appendChild(opt(op, operatorLabel(op))));
        if (!operatorsFor(clause.var).includes(clause.cmp)) clause.cmp = operatorsFor(clause.var)[0];
        operator.value = clause.cmp;
    };
    rebuildOperators();

    const value = el('input', 'input');
    value.type = 'text';
    value.value = clause.value === undefined ? '' : String(clause.value);
    const syncValueVisibility = () => {
        // is_known / is_unknown take no right-hand side at all.
        value.hidden = clause.cmp === 'is_known' || clause.cmp === 'is_unknown';
    };
    syncValueVisibility();

    variable.addEventListener('change', () => {
        clause.var = variable.value;
        rebuildOperators();
        syncValueVisibility();
    });
    operator.addEventListener('change', () => {
        clause.cmp = operator.value;
        syncValueVisibility();
    });
    value.addEventListener('change', () => { clause.value = value.value; });

    const remove = el('button', 'btn btn--ghost', '×');
    remove.type = 'button';
    remove.addEventListener('click', () => {
        draft.clauses.splice(index, 1);
        renderClauses();
    });

    [variable, operator, value, remove].forEach((n) => row.appendChild(n));
    return row;
}

function currentCondition() {
    if (mode === 'expression') {
        return { condition_text: document.getElementById('rule-expression').value };
    }
    return { condition: conditionFromDraft() };
}

async function runPreview() {
    const result = document.getElementById('rule-preview-result');
    const rows = document.getElementById('rule-preview-rows');
    const error = document.getElementById('rule-condition-error');
    error.textContent = '';
    rows.textContent = '';
    try {
        const data = await api('/api/rules/preview',
            json('POST', { ...currentCondition(), target: draft.target, limit: 25 }));
        result.textContent = t('rules.preview_result', {
            true: data.tally.true, false: data.tally.false, unknown: data.tally.unknown,
        });
        if (data.tally.unknown) rows.appendChild(el('p', 'stat-card__meta', t('rules.unknown_warning')));
        data.results.forEach((entry) => {
            const line = el('div', 'stat-card__meta');
            line.appendChild(el('strong', null, entry.machine));
            line.appendChild(document.createTextNode(` — ${entry.result}`));
            // Show each leaf's actual value: a bare "false" leaves the operator guessing
            // which clause was responsible.
            const detail = entry.detail || {};
            const leaves = detail.nodes || [detail];
            leaves.filter((n) => n && n.var).forEach((leaf) => {
                const shown = leaf.known ? String(leaf.actual) : t('rules.outcome.failed');
                line.appendChild(el('span', null, `  [${leaf.var} = ${leaf.known ? shown : '?'}]`));
            });
            rows.appendChild(line);
        });
        // The preview is also how the two condition views stay in step: whichever one the
        // operator used, the server hands back the canonical AST and its text, and both are
        // written into the draft.
        if (data.condition) {
            const shape = draftFromCondition(data.condition);
            if (shape.flat) { draft.clauses = shape.clauses; draft.join = shape.join; }
            draft.condition_text = data.condition_text;
            document.getElementById('rule-expression').value = data.condition_text;
            if (mode === 'builder') renderClauses();
        }
    } catch (e) {
        error.textContent = e.message;
    }
}

// ---------------------------------------------------------------- actions

function renderActions() {
    const host = document.getElementById('rule-actions');
    host.textContent = '';
    draft.actions.forEach((action) => host.appendChild(actionCard(action, () => {
        draft.actions.splice(draft.actions.indexOf(action), 1);
        renderActions();
    })));
    syncCommandsWarning();
}

// Kept separate from renderActions because it has to run on changes that must NOT rebuild
// the editor: choosing "restart" for a message's Yes is exactly when this warning becomes
// relevant, and re-rendering there would throw away the operator's focus mid-edit. It used
// to be inline, so picking that follow-up left the banner hidden -- the one moment the rule
// starts depending on a switch that is off by default was the one moment it said nothing.
function syncCommandsWarning() {
    const mutates = (list) => (list || []).some(
        (f) => f.type === 'command' || f.type === 'script');
    document.getElementById('rule-commands-warning').hidden =
        commandActionsEnabled || !draft.actions.some(
            (a) => a.type === 'command' || a.type === 'script'
                || Object.values(a.on_response || {}).some(mutates));
}

// One card per action, used at the top level AND inside a message's follow-up rows.
//
// `onRemove` is a callback rather than an index because the same card now lives in two
// different lists -- draft.actions and action.on_response[outcome] -- and an index into "the
// list" stopped being a well-defined thing the moment follow-ups became real actions.
function actionCard(action, onRemove) {
    const card = el('div', 'card');
    card.style.marginBottom = 'var(--space-3)';
    const head = el('div', 'toolbar');
    const spec = actionByName.get(action.type);
    head.appendChild(el('strong', null, (spec && spec.label) || action.type));
    const remove = el('button', 'btn btn--ghost', '×');
    remove.type = 'button';
    remove.addEventListener('click', onRemove);
    head.appendChild(remove);
    card.appendChild(head);
    if (spec && spec.description) card.appendChild(el('p', 'stat-card__meta', spec.description));

    action.params = action.params || {};
    if (action.type === 'alert') {
        card.appendChild(textField(action.params, 'text', t('rules.message_body')));
    } else if (action.type === 'snooze') {
        card.appendChild(numberField(action.params, 'seconds', t('rules.cooldown'), 3600));
    } else if (action.type === 'command') {
        card.appendChild(commandEditor(action.params));
    } else if (action.type === 'script') {
        card.appendChild(scriptEditor(action.params));
    } else if (action.type === 'webhook') {
        card.appendChild(textField(action.params, 'url', 'https://…'));
        card.appendChild(textField(action.params, 'template', t('rules.message_body')));
    } else if (action.type === 'email') {
        const to = el('input', 'input');
        to.type = 'text';
        to.value = (action.params.to || []).join(', ');
        to.placeholder = 'someone@example.com';
        to.style.width = '100%';
        to.addEventListener('change', () => {
            action.params.to = to.value.split(',').map((s) => s.trim()).filter(Boolean);
        });
        card.appendChild(to);
        card.appendChild(textField(action.params, 'subject', t('rules.message_title')));
        card.appendChild(textField(action.params, 'body', t('rules.message_body')));
    } else if (action.type === 'show_message') {
        card.appendChild(messageEditor(action));
    }
    return card;
}

// ---------------------------------------------------------------- params editor
//
// ONE editor, used by top-level actions AND by a message's follow-ups. They render the same
// fields because they ARE the same thing -- the server validates a follow-up through the very
// same _validate_action -- and two implementations would drift the moment one gained a field.
//
// Every control writes on `change`, never at render time. That matters more than it looks:
// numberField below stamps bag[key] while BUILDING the field, so reusing it here would write
// a default into every action merely by opening the rule, and the next save would persist it.
// Opening a rule and saving it unchanged must leave the stored JSON untouched.
function paramsEditor(host, spec, bag) {
    host.textContent = '';
    if (!spec) return host;
    if (!spec.described) {
        host.appendChild(el('p', 'stat-card__meta', t('rules.command_undescribed')));
        return host;
    }
    (spec.params || []).forEach((param) => host.appendChild(paramField(param, bag)));
    if ((spec.one_of || []).length) {
        host.appendChild(el('p', 'stat-card__meta',
            t('rules.command_one_of', { params: spec.one_of.join(', ') })));
    }
    return host;
}

function paramField(param, bag) {
    const wrap = el('div');
    wrap.style.marginTop = 'var(--space-2)';

    if (param.kind === 'bool') {
        const label = el('label', 'stat-card__meta');
        const box = el('input');
        box.type = 'checkbox';
        box.style.marginRight = 'var(--space-2)';
        box.checked = bag[param.name] === true;
        box.addEventListener('change', () => {
            // Only store it when true. An unchecked box means "the agent's default", which is
            // not the same fact as an explicit false, and storing one would freeze it.
            if (box.checked) bag[param.name] = true; else delete bag[param.name];
        });
        label.appendChild(box);
        label.append(param.label);
        wrap.appendChild(label);
        if (param.help) wrap.appendChild(el('p', 'stat-card__meta', param.help));
        return wrap;
    }

    wrap.appendChild(el('label', 'stat-card__meta', param.label + (param.required ? ' *' : '')));

    let input;
    if (param.kind === 'enum') {
        input = el('select', 'input');
        (param.choices || []).forEach((choice) => input.appendChild(opt(choice, choice)));
        input.value = bag[param.name] || param.default || (param.choices || [])[0] || '';
    } else if (param.kind === 'int') {
        input = el('input', 'input');
        input.type = 'number';
        if (param.minimum !== null && param.minimum !== undefined) input.min = param.minimum;
        if (param.maximum !== null && param.maximum !== undefined) input.max = param.maximum;
        // The declared default is the PLACEHOLDER, never the value: an empty box means "let
        // the agent decide", and showing that as an editable 60 would make it look chosen.
        if (param.default !== null && param.default !== undefined) input.placeholder = param.default;
        input.value = bag[param.name] === undefined ? '' : bag[param.name];
    } else if (param.kind === 'text') {
        input = el('textarea', 'input');
        input.rows = 8;
        input.spellcheck = false;
        input.style.fontFamily = 'var(--font-mono)';
        input.value = bag[param.name] || '';
    } else {
        input = el('input', 'input');
        input.type = 'text';
        if (param.default) input.placeholder = param.default;
        input.value = bag[param.name] || '';
    }
    input.style.width = '100%';
    input.style.marginTop = 'var(--space-1)';
    input.addEventListener('change', () => {
        const raw = input.value.trim();
        if (raw === '') { delete bag[param.name]; return; }
        bag[param.name] = param.kind === 'int' ? Number(raw) : input.value;
    });
    wrap.appendChild(input);
    if (param.help) wrap.appendChild(el('p', 'stat-card__meta', param.help));
    return wrap;
}

// The command picker plus whatever that command takes. Re-rendering only the params host on
// change keeps the operator's place in the form.
function commandEditor(params) {
    const wrap = el('div');
    const select = el('select', 'input');
    (catalog.commands || []).forEach((command) => {
        const option = opt(command.name, command.label);
        if (!command.available) {
            // Shown, but unpickable, with the reason. A command that simply vanished from the
            // list is a support question; one greyed out with a reason answers it.
            option.disabled = true;
            option.textContent = `${command.label} — ${command.unavailable_reason}`;
        }
        select.appendChild(option);
    });
    const available = (catalog.commands || []).filter((c) => c.available);
    if (!params.command_type && available.length) params.command_type = available[0].name;
    select.value = params.command_type || '';
    wrap.appendChild(select);

    const spec = commandSpec(select.value);
    if (spec && spec.description) wrap.appendChild(el('p', 'stat-card__meta', spec.description));

    params.params = params.params || {};
    const host = el('div');
    paramsEditor(host, spec, params.params);
    wrap.appendChild(host);

    select.addEventListener('change', () => {
        params.command_type = select.value;
        // A new command means new parameters; keeping the old ones would send `script` to
        // gpupdate and be refused on save with a confusing message.
        params.params = {};
        paramsEditor(host, commandSpec(select.value), params.params);
    });
    return wrap;
}

// A reference to a saved script, plus a value for each input it declares.
function scriptEditor(params) {
    const wrap = el('div');
    if (!(scriptCatalog.scripts || []).length) {
        wrap.appendChild(el('p', 'stat-card__meta', t('rules.no_scripts')));
        return wrap;
    }
    const select = el('select', 'input');
    scriptCatalog.scripts.forEach((script) => {
        const option = opt(script.name, script.label || script.name);
        if (!script.enabled) {
            option.disabled = true;
            option.textContent = `${script.label || script.name} — ${t('rules.script_disabled')}`;
        }
        select.appendChild(option);
    });
    const enabled = scriptCatalog.scripts.filter((x) => x.enabled);
    if (!params.script && enabled.length) params.script = enabled[0].name;
    select.value = params.script || '';
    wrap.appendChild(select);

    const host = el('div');
    wrap.appendChild(host);
    const renderInputs = () => {
        host.textContent = '';
        const script = scriptSpec(select.value);
        if (!script) return;
        if (script.description) host.appendChild(el('p', 'stat-card__meta', script.description));
        params.inputs = params.inputs || {};
        (script.inputs || []).forEach((input) => {
            host.appendChild(paramField({
                name: input.name,
                kind: 'str',
                label: input.label || input.name,
                required: input.required,
                default: input.default,
                help: '',
            }, params.inputs));
        });
        if ((script.variables || []).length) {
            host.appendChild(el('p', 'stat-card__meta',
                t('rules.script_reads', { variables: script.variables.join(', ') })));
        }
    };
    renderInputs();
    select.addEventListener('change', () => {
        params.script = select.value;
        params.inputs = {};
        renderInputs();
    });
    return wrap;
}

function textField(bag, key, placeholder) {
    const input = el('input', 'input');
    input.type = 'text';
    input.placeholder = placeholder;
    input.style.width = '100%';
    input.style.marginTop = 'var(--space-2)';
    input.value = bag[key] || '';
    input.addEventListener('change', () => { bag[key] = input.value; });
    return input;
}

function numberField(bag, key, label, fallback) {
    const wrap = el('label', 'stat-card__meta', label + ' ');
    const input = el('input', 'input');
    input.type = 'number';
    input.min = '0';
    input.style.width = '8rem';
    input.value = bag[key] === undefined ? fallback : bag[key];
    bag[key] = Number(input.value);
    input.addEventListener('change', () => { bag[key] = Number(input.value); });
    wrap.appendChild(input);
    return wrap;
}

// The interactive dialog: title, body, a button preset, and one follow-up list per possible
// answer. Every outcome the message can produce gets a row — including the three that are
// not button presses — because an outcome with nowhere to go is the failure mode this whole
// feature is meant to avoid.
function messageEditor(action) {
    const wrap = el('div');
    wrap.appendChild(textField(action.params, 'title', t('rules.message_title')));
    const body = el('textarea', 'input');
    body.rows = 3;
    body.style.width = '100%';
    body.style.marginTop = 'var(--space-2)';
    body.value = action.params.body || '';
    body.addEventListener('change', () => { action.params.body = body.value; });
    wrap.appendChild(body);
    wrap.appendChild(el('p', 'stat-card__meta', t('rules.insert_variable') + ': {{sys.machine}}, {{sys.uptime_days}}'));

    const presets = catalog.button_presets || {};
    const preset = el('select', 'input');
    Object.keys(presets).forEach((name) => preset.appendChild(opt(name, presets[name].label)));
    preset.value = action.params.preset || 'yes_no_later';
    action.params.preset = preset.value;
    // `params.buttons` used to be deleted on every render, which destroyed hand-authored
    // button labels merely by OPENING the rule. It is left alone now: the preset is what this
    // editor edits, and the server expands it (_validate_buttons prefers `buttons` when both
    // are present, so a rule built by hand keeps its labels).
    preset.addEventListener('change', () => {
        action.params.preset = preset.value;
        renderActions();
    });
    const presetLabel = el('label', 'stat-card__meta', t('rules.message_buttons') + ' ');
    presetLabel.appendChild(preset);
    wrap.appendChild(presetLabel);

    wrap.appendChild(numberField(action.params, 'timeout_seconds', t('rules.message_timeout'), 900));

    action.on_response = action.on_response || {};
    // Every outcome the message can produce gets a row, including the ones that are not button
    // presses. `failed` is what the agent reports when the dialog could not be put on the
    // desktop at all -- the one outcome meaning "they never saw it" -- and it used to be the
    // one outcome a rule could not react to.
    const buttons = (presets[preset.value] || {}).buttons || ['ok'];
    const outcomes = buttons.concat((catalog.outcomes || []).map((o) => o.name));
    const outcomeLabel = new Map((catalog.outcomes || []).map((o) => [o.name, o.label]));

    outcomes.forEach((outcome) => {
        const section = el('div');
        section.style.marginTop = 'var(--space-3)';
        const head = el('div', 'toolbar');
        head.appendChild(el('span', 'stat-card__meta',
            (outcomeLabel.has(outcome)
                ? outcomeLabel.get(outcome)
                : t('rules.on_response') + ' ' + t(`rules.button.${outcome}`)) + ':'));

        // A follow-up is a full action now, chosen from the same vocabulary as a top-level
        // one. It used to be one of four fixed choices whose params were synthesised, so
        // "Yes -> run this script" and "Yes -> restart in five minutes" were both unsayable.
        const kind = el('select', 'input');
        (catalog.action_types || []).filter((a) => a.nestable)
            .forEach((a) => kind.appendChild(opt(a.name, a.label)));
        head.appendChild(kind);
        const add = el('button', 'btn btn--ghost', t('rules.add_action'));
        add.type = 'button';
        head.appendChild(add);
        section.appendChild(head);

        const list = el('div');
        list.style.marginLeft = 'var(--space-4)';
        section.appendChild(list);

        const renderFollowups = () => {
            list.textContent = '';
            (action.on_response[outcome] || []).forEach((followup) => {
                list.appendChild(actionCard(followup, () => {
                    const all = action.on_response[outcome];
                    all.splice(all.indexOf(followup), 1);
                    if (!all.length) delete action.on_response[outcome];
                    renderFollowups();
                    syncCommandsWarning();
                }));
            });
            if (!(action.on_response[outcome] || []).length) {
                list.appendChild(el('p', 'stat-card__meta', t('rules.nothing')));
            }
        };
        renderFollowups();

        add.addEventListener('click', () => {
            action.on_response[outcome] = action.on_response[outcome] || [];
            action.on_response[outcome].push({ type: kind.value, params: {} });
            renderFollowups();
            syncCommandsWarning();
        });

        wrap.appendChild(section);
    });
    return wrap;
}

// ---------------------------------------------------------------- save

async function saveRule() {
    const error = document.getElementById('rule-save-error');
    error.textContent = '';
    const payload = {
        name: document.getElementById('rule-name').value.trim(),
        description: document.getElementById('rule-description').value.trim(),
        enabled: document.getElementById('rule-enabled').checked,
        target: draft.target,
        actions: draft.actions,
        fire_once_per_match: document.getElementById('rule-fire-once').checked,
        for_seconds: Number(document.getElementById('rule-for').value),
        cooldown_seconds: Number(document.getElementById('rule-cooldown').value),
        max_targets_per_tick: Number(document.getElementById('rule-max-targets').value),
        ...currentCondition(),
    };
    try {
        if (editingId) {
            await api(`/api/rules/${editingId}`, json('PUT', payload));
        } else {
            await api('/api/rules', json('POST', payload));
        }
        closeEditor();
        loadRules();
    } catch (e) {
        error.textContent = e.message;
    }
}

// ---------------------------------------------------------------- wiring

function fillKindSelects() {
    ['rule-target-kind', 'rule-exclude-kind'].forEach((id) => {
        const select = document.getElementById(id);
        select.textContent = '';
        ['all', 'machines', 'ad_ou', 'field'].forEach((kind) => {
            // "Every PC" as an EXCLUSION would exclude everything, which is never what
            // anybody means; leave it out rather than let a rule be built that targets none.
            if (id === 'rule-exclude-kind' && kind === 'all') return;
            select.appendChild(opt(kind, t(`rules.target.${kind}`)));
        });
    });
    const actionSelect = document.getElementById('rule-action-kind');
    actionSelect.textContent = '';
    // From the server, so a new action type appears here without a JS change. `snooze` was
    // renderable but not addable for exactly as long as this list was hand-kept.
    (catalog.action_types || []).forEach(
        (a) => actionSelect.appendChild(opt(a.name, a.label)));
}

document.getElementById('rules-new').addEventListener('click', () => openEditor(null));
document.getElementById('rule-cancel').addEventListener('click', closeEditor);
document.getElementById('rule-save').addEventListener('click', saveRule);
document.getElementById('rule-preview').addEventListener('click', runPreview);
document.getElementById('rule-mode-builder').addEventListener('click', () => setMode('builder'));
document.getElementById('rule-mode-expression').addEventListener('click', async () => {
    // Round-trip through the server first, so the text box opens on the canonical rendering
    // of what the builder currently says rather than on a client-side guess at it.
    if (mode === 'builder') await runPreview();
    setMode('expression');
});
document.getElementById('rule-join').addEventListener('change', (e) => { draft.join = e.target.value; });
document.getElementById('rule-add-clause').addEventListener('click', () => {
    const first = catalog.variables[0];
    draft.clauses.push({ var: first ? first.name : 'sys.uptime_days', cmp: '>', value: 0 });
    renderClauses();
});
['rule-target-add', 'rule-exclude-add'].forEach((id) => {
    document.getElementById(id).addEventListener('click', () => {
        const side = id === 'rule-target-add' ? 'include' : 'exclude';
        const kind = document.getElementById(
            side === 'include' ? 'rule-target-kind' : 'rule-exclude-kind').value;
        const selector = { kind };
        if (kind === 'machines') selector.machines = [];
        if (kind === 'ad_ou') { selector.ou = ''; selector.include_children = true; }
        if (kind === 'field') { selector.field = ''; selector.value = ''; }
        draft.target[side].push(selector);
        renderTargets();
        refreshTargetCount();
    });
});
// A day, in seconds. The default deferral when somebody declines a message.
const DECLINE_SNOOZE_SECONDS = 86400;

document.getElementById('rule-action-add').addEventListener('click', () => {
    const kind = document.getElementById('rule-action-kind').value;
    const action = { type: kind, params: {} };
    if (kind === 'show_message') {
        // A new message defers for a day when the person says No.
        //
        // The default matters because the alternative is silence: an unrouted answer is an
        // explicit no-op, so a rule whose condition stays true -- "up for more than 7 days"
        // stays true until somebody reboots -- would re-ask on every cooldown, and clicking
        // No would buy the person nothing at all. Prefilled rather than forced: it is an
        // ordinary snooze action, visible in the editor, with an editable duration, and
        // removable like any other.
        action.on_response = { no: [{ type: 'snooze',
                                      params: { seconds: DECLINE_SNOOZE_SECONDS } }] };
    }
    draft.actions.push(action);
    renderActions();
});

// ---------------------------------------------------------------- scripts
//
// The Scripts panel. Writing needs `issue_commands` -- a script body is code that runs as
// SYSTEM on every machine a rule targets -- so the server tells us in `can_edit` whether this
// operator may, and the page is read-only when they may not. The gate is enforced server-side;
// hiding the buttons is courtesy, not security.

let scriptDraft = null;

function renderScripts() {
    const body = document.getElementById('scripts-body');
    const empty = document.getElementById('scripts-empty');
    const list = scriptCatalog.scripts || [];
    body.textContent = '';
    empty.hidden = list.length > 0;
    document.getElementById('scripts-new').hidden = !scriptCatalog.can_edit;
    document.getElementById('scripts-read-only').hidden = scriptCatalog.can_edit;

    list.forEach((script) => {
        const row = el('tr');
        const nameCell = el('td');
        nameCell.appendChild(el('strong', null, script.label || script.name));
        if (!script.enabled) {
            nameCell.appendChild(document.createTextNode(' '));
            nameCell.appendChild(el('span', 'badge', t('scripts.script_disabled_badge')));
        }
        nameCell.appendChild(el('div', 'stat-card__meta', script.name));
        if (script.description) {
            nameCell.appendChild(el('div', 'stat-card__meta rules-table__description',
                                    script.description));
        }
        row.appendChild(nameCell);
        row.appendChild(el('td', 'rules-table__condition', script.shell));
        row.appendChild(el('td', null, (script.inputs || []).map((i) => i.name).join(', ')));

        const tools = el('td');
        const toolRow = el('div', 'rules-table__tools');
        tools.appendChild(toolRow);
        if (scriptCatalog.can_edit) {
            const edit = el('button', 'btn btn--ghost', t('rules.edit'));
            edit.type = 'button';
            edit.addEventListener('click', () => openScript(script.name));
            toolRow.appendChild(edit);

            const remove = el('button', 'btn btn--ghost', t('scripts.delete'));
            remove.type = 'button';
            remove.addEventListener('click', () => deleteScript(script.name));
            toolRow.appendChild(remove);
        }
        row.appendChild(tools);
        body.appendChild(row);
    });
}

function blankScript() {
    return { name: '', label: '', description: '', shell: 'powershell', body: '',
             inputs: [], timeout_seconds: 600, enabled: true };
}

async function openScript(name) {
    // The BODY is only readable with issue_commands, so it comes from the single-script
    // endpoint rather than the list -- the list is deliberately metadata-only.
    scriptDraft = name ? await api('/api/rules/scripts/' + encodeURIComponent(name))
                       : blankScript();
    const shell = document.getElementById('script-shell');
    shell.textContent = '';
    (scriptCatalog.shells || ['powershell']).forEach((x) => shell.appendChild(opt(x, x)));

    document.getElementById('script-name').value = scriptDraft.name || '';
    // The reference name is what rules store, so changing it would silently repoint nothing.
    // Renaming is deliberately delete-and-recreate, which the in-use check then covers.
    document.getElementById('script-name').disabled = !!name;
    document.getElementById('script-label').value = scriptDraft.label || '';
    document.getElementById('script-description').value = scriptDraft.description || '';
    document.getElementById('script-enabled').checked = scriptDraft.enabled !== false;
    shell.value = scriptDraft.shell || 'powershell';
    document.getElementById('script-timeout').value = scriptDraft.timeout_seconds || 600;
    document.getElementById('script-body').value = scriptDraft.body || '';
    document.getElementById('script-save-error').textContent = '';
    renderScriptInputs();
    document.getElementById('script-editor').hidden = false;
}

function closeScript() {
    scriptDraft = null;
    document.getElementById('script-editor').hidden = true;
}

function renderScriptInputs() {
    const host = document.getElementById('script-inputs');
    host.textContent = '';
    (scriptDraft.inputs || []).forEach((input) => {
        const row = el('div', 'toolbar');
        row.style.marginTop = 'var(--space-2)';

        const name = el('input', 'input');
        name.type = 'text';
        name.placeholder = t('scripts.input_name');
        name.value = input.name || '';
        name.addEventListener('change', () => { input.name = name.value.trim().toLowerCase(); });
        row.appendChild(name);

        const label = el('input', 'input');
        label.type = 'text';
        label.placeholder = t('scripts.input_label');
        label.value = input.label || '';
        label.addEventListener('change', () => { input.label = label.value; });
        row.appendChild(label);

        const def = el('input', 'input');
        def.type = 'text';
        def.placeholder = t('scripts.input_default');
        def.value = input.default || '';
        def.addEventListener('change', () => { input.default = def.value; });
        row.appendChild(def);

        const requiredLabel = el('label', 'stat-card__meta');
        const required = el('input');
        required.type = 'checkbox';
        required.style.marginRight = 'var(--space-2)';
        required.checked = input.required !== false;
        required.addEventListener('change', () => { input.required = required.checked; });
        requiredLabel.appendChild(required);
        requiredLabel.append(t('scripts.input_required'));
        row.appendChild(requiredLabel);

        const remove = el('button', 'btn btn--ghost', '×');
        remove.type = 'button';
        remove.addEventListener('click', () => {
            scriptDraft.inputs.splice(scriptDraft.inputs.indexOf(input), 1);
            renderScriptInputs();
        });
        row.appendChild(remove);
        host.appendChild(row);
    });
}

async function saveScript() {
    const error = document.getElementById('script-save-error');
    error.textContent = '';
    const payload = {
        name: document.getElementById('script-name').value.trim().toLowerCase(),
        label: document.getElementById('script-label').value,
        description: document.getElementById('script-description').value,
        shell: document.getElementById('script-shell').value,
        body: document.getElementById('script-body').value,
        timeout_seconds: Number(document.getElementById('script-timeout').value),
        enabled: document.getElementById('script-enabled').checked,
        inputs: (scriptDraft.inputs || []).filter((i) => i.name),
    };
    try {
        await api('/api/rules/scripts', json('POST', payload));
        closeScript();
        await reloadScripts();
    } catch (e) {
        error.textContent = t('scripts.save_failed', { error: e.message });
    }
}

async function deleteScript(name) {
    if (!window.confirm(t('scripts.confirm_delete'))) return;
    try {
        await api('/api/rules/scripts/' + encodeURIComponent(name), { method: 'DELETE' });
        await reloadScripts();
    } catch (e) {
        // The 409 body names the rules using it, which is the whole point of refusing.
        window.alert(t('scripts.delete_failed', { error: e.message }));
    }
}

async function reloadScripts() {
    scriptCatalog = await api('/api/rules/scripts');
    scriptByName = new Map((scriptCatalog.scripts || []).map((x) => [x.name, x]));
    renderScripts();
}

document.getElementById('scripts-new').addEventListener('click', () => openScript(null));
document.getElementById('script-cancel').addEventListener('click', closeScript);
document.getElementById('script-save').addEventListener('click', saveScript);
document.getElementById('script-input-add').addEventListener('click', () => {
    scriptDraft.inputs = scriptDraft.inputs || [];
    scriptDraft.inputs.push({ name: '', label: '', required: true, default: '' });
    renderScriptInputs();
});

(async function start() {
    await loadCatalog();
    fillKindSelects();
    renderScripts();
    await loadRules();
})();
