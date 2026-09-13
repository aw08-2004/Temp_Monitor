// Model list + provider help for the Settings -> AI tab (roadmap #24).
//
// The ai.* fields themselves are schema-rendered by settings.js like every other section.
// This adds the two things a schema field cannot express:
//
//   * a "Refresh the list" button. A provider's model ids are a third party's vocabulary --
//     `llama3.1:8b`, `anthropic/claude-sonnet-4.5`, `gpt-4o-mini` -- and getting one wrong
//     fails at the first draft, an hour later, as somebody else's 404. Reading the list from
//     the provider turns that into a choice.
//   * an autocomplete on the model box, fed from that cached list. Deliberately an
//     AUTOCOMPLETE and not a <select>: OpenRouter serves hundreds of models, the list can be
//     stale or unreachable, and a gateway may serve one this hub has never heard of -- so the
//     list has to suggest without ever being the only way to answer.
//
// It re-injects on 'settings:rendered' because settings.js replaces the panel's children on
// load and after every save, which would otherwise wipe both.
(function () {
    'use strict';

    const PANEL_ID = 'tab-ai';
    const CARD_ID = 'ai-models-card';
    const MODEL_INPUT_ID = 'set-ai-model';
    let listing = null;      // the last /api/ai/models answer
    let status = null;       // the last /api/ai/status answer
    let busy = false;
    let message = '';        // the outcome of a refresh pressed this session
    let messageIsError = false;

    function el(tag, props, children) {
        const node = document.createElement(tag);
        if (props) {
            for (const [k, v] of Object.entries(props)) {
                if (k === 'class') node.className = v;
                else if (k === 'text') node.textContent = v;
                else node.setAttribute(k, v);
            }
        }
        for (const child of children || []) {
            if (child) {
                node.appendChild(typeof child === 'string'
                    ? document.createTextNode(child) : child);
            }
        }
        return node;
    }

    async function api(path, options) {
        const resp = await fetch(path, options);
        let body = null;
        try { body = await resp.json(); } catch (e) { /* an empty body is fine */ }
        if (!resp.ok) throw new Error((body && body.error) || `HTTP ${resp.status}`);
        return body;
    }

    function ageText(cachedAt) {
        // Relative, because "read 4m ago" answers the question somebody actually has -- is
        // this list worth trusting -- while a wall-clock time makes them do the sum.
        //
        // `machine.ago.*` rather than a new settings.ago.*: the catalog already carries these
        // four strings in three languages, and a second home for "{value}h ago" is how one of
        // them ends up translated and the other does not. The key lives under `machine`
        // because that page needed it first, not because it is about a machine.
        const seconds = Math.max(0, Math.round(Date.now() / 1000 - cachedAt));
        if (seconds < 90) return t('machine.ago.seconds', { value: seconds });
        const minutes = Math.round(seconds / 60);
        if (minutes < 90) return t('machine.ago.minutes', { value: minutes });
        const hours = Math.round(minutes / 60);
        if (hours < 36) return t('machine.ago.hours', { value: hours });
        return t('machine.ago.days', { value: Math.round(hours / 24) });
    }

    function summaryLine() {
        if (!listing || !listing.models.length) return t('settings.ai.never');
        const params = { count: listing.models.length, age: ageText(listing.cached_at) };
        return listing.stale ? t('settings.ai.stale', params) : t('settings.ai.count', params);
    }

    function buildCard() {
        const card = el('div', { class: 'card', id: CARD_ID,
            style: 'margin-bottom: var(--space-4);' }, [
            el('h3', { class: 'section-title', text: t('settings.ai.models_title') }),
            el('p', { class: 'setting__help', text: summaryLine() }),
        ]);

        // A hosted provider with no key cannot answer anything, and the failure arrives as a
        // 401 that reads like a broken hub. Said here, before the button is pressed.
        if (status && !status.has_api_key && listing
            && (listing.provider === 'openai' || listing.provider === 'openrouter')) {
            card.appendChild(el('p', { class: 'setting__help',
                text: t('settings.ai.needs_key') }));
        }
        if (listing && listing.provider === 'custom') {
            card.appendChild(el('p', { class: 'setting__help',
                text: t('settings.ai.custom_hint') }));
        }

        if (listing && listing.can_refresh) {
            const button = el('button', { class: 'btn btn--ghost', type: 'button',
                text: busy ? t('settings.ai.refreshing') : t('settings.ai.refresh') });
            if (busy) button.setAttribute('disabled', 'disabled');
            button.addEventListener('click', refreshModels);
            card.appendChild(button);
        }
        if (message) {
            const line = el('p', { class: 'setting__help', text: message });
            if (messageIsError) line.style.color = 'var(--color-danger, #f85149)';
            card.appendChild(line);
        }
        return card;
    }

    async function refreshModels() {
        busy = true;
        message = '';
        inject();
        try {
            listing = await api('/api/ai/models/refresh', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: '{}',
            });
            messageIsError = false;
        } catch (e) {
            // The server's sentence, not a generic one: it distinguishes a missing key from
            // an unreachable host from a provider that listed nothing, and each has a
            // different fix.
            message = t('settings.ai.failed', { error: e.message });
            messageIsError = true;
        }
        busy = false;
        inject();
    }

    function attachModelPicker() {
        const input = document.getElementById(MODEL_INPUT_ID);
        if (!input || !listing || !listing.models.length) return;
        if (input.dataset.aiPicker === '1') return;
        if (!window.attachAutocomplete) return;   // vendored script missing; free text still works
        input.dataset.aiPicker = '1';
        window.attachAutocomplete(input, {
            minChars: 0,
            emptyText: t('settings.ai.never'),
            source: (query) => {
                const needle = String(query || '').toLowerCase();
                return listing.models
                    .filter((id) => !needle || id.toLowerCase().includes(needle))
                    .slice(0, 50)
                    .map((id) => ({ value: id, label: id }));
            },
            onSelect: (item) => {
                input.value = item.value;
                // settings.js listens for `input` to mark the field dirty and `change` to
                // save. A programmatic assignment fires neither, so a picked model would
                // look chosen and never be stored.
                input.dispatchEvent(new Event('input', { bubbles: true }));
                input.dispatchEvent(new Event('change', { bubbles: true }));
            },
        });
    }

    function inject() {
        const panel = document.getElementById(PANEL_ID);
        if (!panel || !listing) return;
        const existing = document.getElementById(CARD_ID);
        if (existing) existing.remove();
        // Prepended: which provider and which models come before the timeouts and the
        // retention window, and the model box below is what the card is about.
        panel.insertBefore(buildCard(), panel.firstChild);
        attachModelPicker();
    }

    async function load() {
        try {
            [status, listing] = await Promise.all([
                api('/api/ai/status'),
                api('/api/ai/models'),
            ]);
        } catch (e) {
            return;     // the AI section is optional; its card never takes the page down
        }
        inject();
    }

    document.addEventListener('settings:rendered', inject);
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', load);
    } else {
        load();
    }
})();
