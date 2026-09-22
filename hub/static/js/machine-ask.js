// The machine page's "ask about this machine" fold -- roadmap #24.
//
// **The answer and the readings it was built from are rendered together, and that is the whole
// design of this file.** Everything else the console shows a model's output for -- a drafted
// rule, a fleet query -- is checked by the engine before it arrives: an expression that did not
// parse never reaches the page. A paragraph cannot be checked that way, so the only thing
// standing between a confident sentence and an operator acting on it is the person reading it,
// and they can only do that job if the numbers are underneath the sentence rather than in a
// different tab. An answer citing `metric.cpu_load_pct = 96` beside a row reading 12 is caught
// in a glance; the same answer alone is not caught at all.
//
// Two things this file deliberately does not do:
//
//  * It does not keep a conversation. Each question is answered against the machine's CURRENT
//    readings, and a thread would let an answer built on values from four minutes ago be
//    refined as though it were still true. The readings move; the transcript would not.
//  * It does not render the answer as anything but text. `textContent`, never innerHTML --
//    this string came from a third party's model, and it is the one string on this page that
//    did.
//
// What the hub sent is stated in words above the box rather than left to be inferred, because
// `ai.send_machine_names` is off by default and "did this PC's name leave the building" is a
// question somebody will be asked by their own management one day.
(function () {
    'use strict';

    const fold = document.getElementById('card-ask');
    if (!fold || !window.MachineContext) return;

    const machine = window.MachineContext.current();
    if (!machine) return;

    const textEl = document.getElementById('ask-text');
    const sendEl = document.getElementById('ask-send');
    const statusEl = document.getElementById('ask-status');
    const answerEl = document.getElementById('ask-answer');
    const readingsEl = document.getElementById('ask-readings');
    const snapshotEl = document.getElementById('ask-snapshot');
    const disclosureEl = document.getElementById('ask-disclosure');

    async function api(path, options) {
        const resp = await fetch(path, options);
        let data = null;
        try { data = await resp.json(); } catch (e) { /* empty body is fine */ }
        if (!resp.ok) throw new Error((data && data.error) || `HTTP ${resp.status}`);
        return data;
    }

    function json(method, payload) {
        // Content-Type: application/json is load-bearing, not cosmetic -- it is what makes a
        // cross-origin POST preflight and fail. See fleet_web.py's module docstring.
        return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) };
    }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    /** Seconds since a reading, as words. A bare number of seconds is a figure somebody has to
     *  convert before it means anything, and this column exists to be glanced at. */
    function age(seconds) {
        if (seconds === null || seconds === undefined) return '';
        const total = Math.max(0, Math.round(Number(seconds) || 0));
        if (total < 90) return t('ai.age_seconds', { seconds: total });
        if (total < 5400) return t('ai.age_minutes', { minutes: Math.round(total / 60) });
        return t('ai.age_hours', { hours: Math.round(total / 3600) });
    }

    function renderSnapshot(rows) {
        snapshotEl.textContent = '';
        const table = el('table', 'data-table');
        const head = el('thead');
        const headRow = el('tr');
        [t('ai.column.variable'), t('ai.column.value'), t('ai.column.age')]
            .forEach((label) => headRow.appendChild(el('th', null, label)));
        head.appendChild(headRow);
        table.appendChild(head);

        const body = el('tbody');
        rows.forEach((row) => {
            const tr = el('tr');
            tr.appendChild(el('td', null, row.name));
            // `value` already reads "(withheld)" or "unknown" server-side -- the hub decides
            // what a withheld entry looks like, so a console that spelled it differently would
            // be describing a redaction it did not perform.
            tr.appendChild(el('td', null, row.value));
            tr.appendChild(el('td', 'stat-card__meta', age(row.age_seconds)));
            body.appendChild(tr);
        });
        table.appendChild(body);
        snapshotEl.appendChild(table);
    }

    function renderAnswer(data) {
        answerEl.textContent = data.answer || '';
        renderSnapshot(data.snapshot || []);
        readingsEl.hidden = false;
        // Restated per answer rather than once at load: the setting is read per request on the
        // hub, so a colleague changing it in Settings takes effect on the next question and
        // this line has to say what was true for THIS one.
        disclosureEl.textContent = data.sent_machine_name
            ? t('ai.sent_with_names')
            : t('ai.sent_without_names', { withheld: data.withheld || 0 });
    }

    async function ask() {
        const text = textEl.value.trim();
        if (!text) return;
        statusEl.textContent = t('ai.asking');
        sendEl.disabled = true;
        try {
            const data = await api(
                `/api/ai/machines/${encodeURIComponent(machine)}/ask`, json('POST', { text }));
            renderAnswer(data);
            statusEl.textContent = '';
        } catch (e) {
            // The server's sentence, not a generic one: ai_web.py is what decides which errors
            // are safe to send, and "the AI provider did not answer in time" is something an
            // operator can act on where "failed" is not.
            statusEl.textContent = t('ai.failed', { error: e.message });
        } finally {
            sendEl.disabled = false;
        }
    }

    sendEl.addEventListener('click', ask);
    textEl.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') { event.preventDefault(); ask(); }
    });

    // The card exists only where the feature does. A failure here leaves it hidden rather than
    // taking the machine page with it -- the drafter on the Rules page makes the same choice,
    // and for the same reason: this is an extra, and a hub with no provider configured is the
    // normal case.
    (async function reveal() {
        let status = null;
        try {
            status = await api('/api/ai/status');
        } catch (e) {
            return;
        }
        if (!status || !status.ready) return;
        disclosureEl.textContent = status.send_machine_names
            ? t('ai.sends_names')
            : t('ai.withholds_names');
        fold.hidden = false;
    })();
})();
