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

const ACTION_KINDS = ['show_message', 'alert', 'command', 'webhook', 'email'];
// Commands a rule may issue. Mirrors rules.RULE_ALLOWED_COMMANDS minus the ones whose
// params a rule cannot meaningfully supply; the server is the authority and refuses
// anything else, so a drift here costs an error message, not correctness.
const RULE_COMMANDS = ['restart', 'shutdown', 'gpupdate', 'run_script', 'install_app',
                       'prepare_wake', 'refresh_bios_inventory'];

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

async function loadCatalog() {
    catalog = await api('/api/rules/variables');
    byName = new Map(catalog.variables.map((v) => [v.name, v]));
}

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
        if (rule.description) nameCell.appendChild(el('div', 'stat-card__meta', rule.description));
        row.appendChild(nameCell);
        row.appendChild(el('td', null, rule.condition_text));
        row.appendChild(el('td', null, (rule.actions || [])
            .map((a) => t(`rules.action.${a.type}.label`)).join(', ')));
        row.appendChild(el('td', null, String(rule.matching || 0)));

        const tools = el('td');
        if (canManage) {
            const edit = el('button', 'btn btn--ghost', t('rules.edit'));
            edit.type = 'button';
            edit.addEventListener('click', () => openEditor(rule));
            tools.appendChild(edit);

            const toggle = el('button', 'btn btn--ghost',
                rule.enabled ? t('rules.disabled') : t('rules.enabled'));
            toggle.type = 'button';
            toggle.addEventListener('click', async () => {
                await api(`/api/rules/${rule.id}/enabled`, json('PUT', { enabled: !rule.enabled }));
                loadRules();
            });
            tools.appendChild(toggle);

            const remove = el('button', 'btn btn--ghost', t('rules.delete'));
            remove.type = 'button';
            remove.addEventListener('click', async () => {
                if (!window.confirm(t('rules.confirm_delete'))) return;
                await api(`/api/rules/${rule.id}`, { method: 'DELETE' });
                loadRules();
            });
            tools.appendChild(remove);
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
    draft.actions.forEach((action, index) => host.appendChild(actionCard(action, index)));
    document.getElementById('rule-commands-warning').hidden =
        commandActionsEnabled || !draft.actions.some((a) => a.type === 'command'
            || Object.values(a.on_response || {}).some(
                (list) => (list || []).some((f) => f.type === 'command')));
}

function actionCard(action, index) {
    const card = el('div', 'card');
    card.style.marginBottom = 'var(--space-3)';
    const head = el('div', 'toolbar');
    head.appendChild(el('strong', null, t(`rules.action.${action.type}.label`)));
    const remove = el('button', 'btn btn--ghost', '×');
    remove.type = 'button';
    remove.addEventListener('click', () => { draft.actions.splice(index, 1); renderActions(); });
    head.appendChild(remove);
    card.appendChild(head);
    card.appendChild(el('p', 'stat-card__meta', t(`rules.action.${action.type}.description`)));

    action.params = action.params || {};
    if (action.type === 'alert') {
        card.appendChild(textField(action.params, 'text', t('rules.message_body')));
    } else if (action.type === 'snooze') {
        card.appendChild(numberField(action.params, 'seconds', t('rules.cooldown'), 3600));
    } else if (action.type === 'command') {
        const select = el('select', 'input');
        RULE_COMMANDS.forEach((c) => select.appendChild(opt(c, c)));
        select.value = action.params.command_type || 'restart';
        action.params.command_type = select.value;
        select.addEventListener('change', () => { action.params.command_type = select.value; });
        card.appendChild(select);
        if (!canIssueCommands) {
            card.appendChild(el('p', 'stat-card__meta', t('rules.action.command.description')));
        }
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

    const preset = el('select', 'input');
    ['ok', 'ok_cancel', 'yes_no', 'yes_no_later', 'accept_decline']
        .forEach((p) => preset.appendChild(opt(p, t(`rules.buttons.${p}`))));
    preset.value = action.params.preset || 'yes_no_later';
    action.params.preset = preset.value;
    delete action.params.buttons;
    preset.addEventListener('change', () => {
        action.params.preset = preset.value;
        delete action.params.buttons;
        renderActions();
    });
    const presetLabel = el('label', 'stat-card__meta', t('rules.message_buttons') + ' ');
    presetLabel.appendChild(preset);
    wrap.appendChild(presetLabel);

    wrap.appendChild(numberField(action.params, 'timeout_seconds', t('rules.message_timeout'), 900));

    action.on_response = action.on_response || {};
    const outcomes = (PRESET_BUTTONS[preset.value] || ['ok'])
        .concat(['timeout', 'dismissed', 'no_session']);
    outcomes.forEach((outcome) => {
        const row = el('div', 'toolbar');
        row.style.marginTop = 'var(--space-2)';
        const isButton = !!PRESET_BUTTON_SET.has(outcome);
        row.appendChild(el('span', 'stat-card__meta',
            (isButton ? t('rules.on_response') + ' ' + t(`rules.button.${outcome}`)
                      : t(`rules.outcome.${outcome}`)) + ':'));
        const choice = el('select', 'input');
        choice.appendChild(opt('', t('rules.nothing')));
        choice.appendChild(opt('restart', t('rules.action.command.label') + ': restart'));
        choice.appendChild(opt('shutdown', t('rules.action.command.label') + ': shutdown'));
        choice.appendChild(opt('snooze', t('rules.action.snooze.label')));
        choice.appendChild(opt('alert', t('rules.action.alert.label')));
        choice.value = followupKey(action.on_response[outcome]);
        choice.addEventListener('change', () => {
            action.on_response[outcome] = followupFor(choice.value, outcome);
        });
        row.appendChild(choice);
        wrap.appendChild(row);
    });
    return wrap;
}

const PRESET_BUTTONS = {
    ok: ['ok'],
    ok_cancel: ['ok', 'cancel'],
    yes_no: ['yes', 'no'],
    yes_no_later: ['yes', 'no', 'later'],
    accept_decline: ['accept', 'decline'],
};
const PRESET_BUTTON_SET = new Set(['ok', 'cancel', 'yes', 'no', 'later', 'accept', 'decline']);

function followupKey(list) {
    if (!list || !list.length) return '';
    const first = list[0];
    if (first.type === 'command') return first.params.command_type;
    return first.type;
}

function followupFor(key, outcome) {
    if (!key) return [];
    if (key === 'snooze') {
        // "Later" defers longer than a timeout does: one is somebody actively asking for
        // more time, the other is nobody having been there.
        return [{ type: 'snooze', params: { seconds: outcome === 'later' ? 14400 : 3600 } }];
    }
    if (key === 'alert') {
        return [{ type: 'alert', params: { text: '{{sys.machine}}: ' + outcome } }];
    }
    return [{ type: 'command', params: { command_type: key, params: {} } }];
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
    ACTION_KINDS.forEach((kind) => actionSelect.appendChild(opt(kind, t(`rules.action.${kind}.label`))));
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
document.getElementById('rule-action-add').addEventListener('click', () => {
    const kind = document.getElementById('rule-action-kind').value;
    draft.actions.push({ type: kind, params: {} });
    renderActions();
});

(async function start() {
    await loadCatalog();
    fillKindSelects();
    await loadRules();
})();
