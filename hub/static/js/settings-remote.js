// TURN/STUN status + secret control for the Settings -> Remote Control tab.
//
// The schema-driven fields (remote.stun_urls, remote.turn_urls, consent, TTLs) are rendered by
// settings.js like every other section. This script adds the one thing a schema field can't
// express: a live read of whether remote sessions will actually connect -- is REMOTE_TURN_SECRET
// set, are URLs configured, and how many ICE servers a session hands a peer right now (0 is the
// 'ice_servers=0 -> peer failed' case) -- plus a control to set/rotate the secret without shell
// access on the hub host.
//
// It re-injects on the 'settings:rendered' event because settings.js replaces the panel's
// children on load and after every save, which would otherwise wipe the card.
(function () {
    'use strict';

    const PANEL_ID = 'tab-remote';
    const CARD_ID = 'turn-status-card';
    let cachedStatus = null;
    let lastSecretShown = null;   // shown once after a set/rotate, kept across re-renders

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
            if (child) node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
        }
        return node;
    }

    function pill(text, kind) {
        return el('span', { class: 'status-pill status-pill--' + kind }, [
            el('span', { class: 'status-pill__dot' }), text,
        ]);
    }

    function statRow(label, valueNode) {
        return el('div', { class: 'turn-stat' }, [
            el('span', { class: 'turn-stat__label', text: label }),
            el('span', { class: 'turn-stat__value' }, [valueNode]),
        ]);
    }

    async function postSecret(secret) {
        const resp = await fetch('/api/remote/turn/secret', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ secret: secret || '' }),
        });
        const body = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(body.error || ('HTTP ' + resp.status));
        return body;
    }

    function secretControls() {
        const canWrite = cachedStatus && cachedStatus.can_write_secret;
        const wrap = el('div', { class: 'turn-secret' });

        if (!canWrite) {
            wrap.appendChild(el('p', { class: 'setting__help', text:
                'This deployment cannot write .env from the hub; set REMOTE_TURN_SECRET on the '
                + 'host and restart the hub.' }));
            return wrap;
        }

        const input = el('input', {
            type: 'text', class: 'input', placeholder: 'Paste an existing coturn secret, or leave blank to generate',
            autocomplete: 'off', spellcheck: 'false',
        });
        const setBtn = el('button', { type: 'button', class: 'btn btn--primary', text: 'Set secret' });
        const rotBtn = el('button', { type: 'button', class: 'btn btn--ghost', text: 'Rotate (generate)' });
        const msg = el('div', { class: 'turn-secret__msg' });

        async function submit(useInput) {
            setBtn.disabled = rotBtn.disabled = true;
            msg.textContent = '';
            msg.className = 'turn-secret__msg';
            try {
                const body = await postSecret(useInput ? input.value.trim() : '');
                lastSecretShown = body.secret;
                input.value = '';
                await refresh();     // re-render with secret_set = true + the reveal box
            } catch (e) {
                msg.textContent = e.message;
                msg.className = 'turn-secret__msg turn-secret__msg--err';
                setBtn.disabled = rotBtn.disabled = false;
            }
        }
        setBtn.addEventListener('click', () => submit(true));
        rotBtn.addEventListener('click', () => submit(false));

        wrap.appendChild(el('label', { class: 'setting__label', text: 'TURN shared secret (REMOTE_TURN_SECRET)' }));
        wrap.appendChild(input);
        wrap.appendChild(el('div', { class: 'card-actions' }, [setBtn, rotBtn]));
        wrap.appendChild(msg);

        if (lastSecretShown) {
            const box = el('div', { class: 'turn-secret__reveal' });
            box.appendChild(el('p', { class: 'setting__help', text:
                'Saved and applied to the hub. Copy it now -- it is not shown again. coturn '
                + 'validates against its OWN copy, so set the SAME value as its '
                + '--static-auth-secret (turn/.env REMOTE_TURN_SECRET) and restart coturn, or '
                + 'TURN will reject every allocation.' }));
            const code = el('input', { type: 'text', class: 'input turn-secret__code', readonly: 'readonly' });
            code.value = lastSecretShown;
            code.addEventListener('focus', () => code.select());
            box.appendChild(code);
            wrap.appendChild(box);
        }
        return wrap;
    }

    function buildCard() {
        const card = el('div', { class: 'card', id: CARD_ID });
        card.appendChild(el('h3', { class: 'card__title', text: 'TURN / STUN status' }));

        if (!cachedStatus) {
            card.appendChild(el('p', { class: 'setting__help', text: 'Loading TURN status...' }));
            return card;
        }

        const s = cachedStatus;
        const iceKind = s.ice_count > 0 ? 'ok' : (s.enabled ? 'danger' : 'muted');
        const stats = el('div', { class: 'turn-stats' }, [
            statRow('Remote control', s.enabled ? pill('On', 'ok') : pill('Off', 'muted')),
            statRow('REMOTE_TURN_SECRET', s.secret_set ? pill('Set', 'ok') : pill('Not set', 'warn')),
            statRow('STUN servers', el('span', { text: String(s.stun_count) })),
            statRow('TURN servers', el('span', { text: String(s.turn_count) })),
            statRow('ICE servers per session', pill(String(s.ice_count), iceKind)),
        ]);
        card.appendChild(stats);

        if (s.ice_count === 0 && s.enabled) {
            card.appendChild(el('p', { class: 'turn-warn', text:
                'A session hands peers 0 ICE servers, so anything not on the same LAN fails to '
                + 'connect (the agent logs "ice_servers=0"). Add a STUN and/or TURN URL below and, '
                + 'for TURN, set the shared secret.' }));
        }

        card.appendChild(secretControls());
        card.appendChild(el('p', { class: 'setting__help', text:
            'Point TURN servers at the hub\'s coturn, e.g. turn:your-hub:3478 (and stun:your-hub:3478). '
            + 'The URLs are the fields below; this secret is what mints per-session credentials.' }));
        return card;
    }

    function render() {
        const panel = document.getElementById(PANEL_ID);
        if (!panel) return;
        const existing = document.getElementById(CARD_ID);
        if (existing) existing.remove();
        panel.insertBefore(buildCard(), panel.firstChild);
    }

    async function refresh() {
        try {
            const resp = await fetch('/api/remote/turn/status');
            if (!resp.ok) return;            // page already requires manage_settings; bail quietly
            cachedStatus = await resp.json();
        } catch (e) { /* leave cachedStatus as-is */ }
        render();
    }

    // Re-inject after settings.js (re)renders the panel; refresh once on initial load.
    document.addEventListener('settings:rendered', render);
    document.addEventListener('DOMContentLoaded', refresh);
})();
