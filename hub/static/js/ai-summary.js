// The Alerts page's fleet summary -- roadmap #24.
//
// **The figures and the paragraph are two different kinds of thing, and this file renders them
// as two different kinds of thing.** Every number here was computed by the hub from its own
// alerts and rule-fire tables; the paragraph above them was written by a language model asked
// for a covering note on those numbers and nothing else. So the list is always drawn and the
// paragraph is sometimes missing -- a provider that is off, unreachable or out of credit costs
// the sentence and not the report. The reverse arrangement, a summary that exists only as
// prose, is the one that gets a made-up figure quoted into a change ticket.
//
// Nothing here polls. A report is read when somebody opens it, and a card that re-asked a paid
// provider every thirty seconds while a page sat open in a forgotten tab would be a bill
// nobody authorised.
(function () {
    'use strict';

    const fold = document.getElementById('ai-summary');
    if (!fold) return;

    const runEl = document.getElementById('ai-summary-run');
    const windowEl = document.getElementById('ai-summary-window');
    const statusEl = document.getElementById('ai-summary-status');
    const proseEl = document.getElementById('ai-summary-prose');
    const linesEl = document.getElementById('ai-summary-lines');

    let loaded = false;

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

    function render(data) {
        proseEl.textContent = data.prose || '';
        linesEl.textContent = '';
        (data.lines || []).forEach((line) => {
            const item = document.createElement('li');
            item.textContent = line;
            linesEl.appendChild(item);
        });
        // A missing paragraph is stated rather than left as a blank space: the figures are
        // complete, and an operator should know the covering note is absent because a provider
        // did not answer rather than because there was nothing to say.
        statusEl.textContent = data.prose_error
            ? t('ai.summary_no_prose', { error: data.prose_error })
            : '';
    }

    async function load() {
        const days = Number(windowEl.value) || 1;
        statusEl.textContent = t('ai.summary_running');
        runEl.disabled = true;
        try {
            render(await api('/api/ai/summary', json('POST', { window_days: days })));
            loaded = true;
        } catch (e) {
            statusEl.textContent = t('ai.failed', { error: e.message });
        } finally {
            runEl.disabled = false;
        }
    }

    runEl.addEventListener('click', load);
    windowEl.addEventListener('change', () => { if (loaded) load(); });
    // Fetched on the first open rather than on page load, and once per window choice. The
    // section is a plain disclosure with no remembered state (see alerts.html), so `toggle`
    // only ever fires because somebody clicked it -- which is the whole reason a paid request
    // is safe to hang off it.
    fold.addEventListener('toggle', () => {
        if (fold.open && !loaded) load();
    });

    // Shown only where the feature is configured, like the drafter on the Rules page. The
    // figures alone would work without a provider, but a card whose covering note is
    // permanently missing on a hub that never had AI is a card that only raises questions.
    (async function reveal() {
        try {
            const status = await api('/api/ai/status');
            if (status && status.ready) fold.hidden = false;
        } catch (e) {
            /* an extra, not the page -- leave it hidden */
        }
    })();
})();
