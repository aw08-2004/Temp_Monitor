// TURN/STUN status, secret control, and the virtual-display payload pin for the
// Settings -> Remote Control tab.
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
    const VDD_CARD_ID = 'virtual-display-card';
    let cachedStatus = null;
    let cachedPayload = null;     // the pinned virtual display driver, or null
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
            wrap.appendChild(el('p', { class: 'setting__help',
                text: t('settings.remote.cannot_write_env') }));
            return wrap;
        }

        const input = el('input', {
            type: 'text', class: 'input', placeholder: t('settings.remote.secret_placeholder'),
            autocomplete: 'off', spellcheck: 'false',
        });
        const setBtn = el('button', { type: 'button', class: 'btn btn--primary',
                                     text: t('settings.remote.set_secret') });
        const rotBtn = el('button', { type: 'button', class: 'btn btn--ghost',
                                     text: t('settings.remote.rotate_secret') });
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

        wrap.appendChild(el('label', { class: 'setting__label',
                                       text: t('settings.remote.secret_label') }));
        wrap.appendChild(input);
        wrap.appendChild(el('div', { class: 'card-actions' }, [setBtn, rotBtn]));
        wrap.appendChild(msg);

        if (lastSecretShown) {
            const box = el('div', { class: 'turn-secret__reveal' });
            box.appendChild(el('p', { class: 'setting__help',
                text: t('settings.remote.secret_saved') }));
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
        card.appendChild(el('h3', { class: 'card__title', text: t('settings.remote.title') }));

        if (!cachedStatus) {
            card.appendChild(el('p', { class: 'setting__help',
                                       text: t('settings.remote.loading') }));
            return card;
        }

        const s = cachedStatus;
        const iceKind = s.ice_count > 0 ? 'ok' : (s.enabled ? 'danger' : 'muted');
        const stats = el('div', { class: 'turn-stats' }, [
            statRow(t('settings.remote.remote_control'),
                    s.enabled ? pill(t('settings.remote.on'), 'ok')
                              : pill(t('settings.remote.off'), 'muted')),
            // The .env key name is deliberately NOT translated: it is what you type.
            statRow('REMOTE_TURN_SECRET',
                    s.secret_set ? pill(t('settings.remote.secret_set'), 'ok')
                                 : pill(t('settings.remote.secret_unset'), 'warn')),
            statRow(t('settings.remote.stun_servers'), el('span', { text: String(s.stun_count) })),
            statRow(t('settings.remote.turn_servers'), el('span', { text: String(s.turn_count) })),
            statRow(t('settings.remote.ice_per_session'), pill(String(s.ice_count), iceKind)),
        ]);
        card.appendChild(stats);

        if (s.ice_count === 0 && s.enabled) {
            card.appendChild(el('p', { class: 'turn-warn',
                text: t('settings.remote.no_ice_warning') }));
        }

        card.appendChild(secretControls());
        card.appendChild(el('p', { class: 'setting__help',
            text: t('settings.remote.urls_help') }));
        return card;
    }

    // ---- Virtual display payload ---------------------------------------------------------
    // Which uploaded package blob the fleet installs when an operator clicks "Install virtual
    // display" on a headless machine. Pinning it is a fleet-wide decision about what code the
    // agents will be told to run, so it lives here behind manage_settings rather than next to
    // the per-machine install button, which only needs remote_control.
    function buildVddCard() {
        const card = el('div', { class: 'card', id: VDD_CARD_ID });
        card.appendChild(el('h3', { class: 'card__title', text: t('settings.remote.vdd_title') }));
        card.appendChild(el('p', { class: 'setting__help',
            text: t('settings.remote.vdd_help') }));

        const current = el('div', { class: 'turn-stats' }, [
            statRow(t('settings.remote.vdd_pinned'), cachedPayload
                ? pill(cachedPayload.version, 'ok')
                : pill(t('settings.remote.vdd_none'), 'warn')),
        ]);
        card.appendChild(current);
        if (cachedPayload) {
            card.appendChild(el('p', { class: 'setting__help',
                text: t('settings.remote.vdd_detail', {
                    sha256: cachedPayload.sha256,
                    filename: cachedPayload.filename
                              || t('settings.remote.vdd_payload_fallback'),
                    who: cachedPayload.uploaded_by,
                }) }));
        }

        const version = el('input', { type: 'text', class: 'input',
            placeholder: t('settings.remote.vdd_version_placeholder'), autocomplete: 'off' });
        const digest = el('input', { type: 'text', class: 'input',
            placeholder: t('settings.remote.vdd_sha_placeholder'),
            autocomplete: 'off', spellcheck: 'false' });
        const filename = el('input', { type: 'text', class: 'input',
            placeholder: t('settings.remote.vdd_filename_placeholder'), autocomplete: 'off' });
        const save = el('button', { type: 'button', class: 'btn btn--primary',
                                    text: t('settings.remote.vdd_pin') });
        const msg = el('div', { class: 'turn-secret__msg' });

        save.addEventListener('click', async () => {
            save.disabled = true;
            msg.textContent = '';
            msg.className = 'turn-secret__msg';
            try {
                const resp = await fetch('/api/remote/virtual-display/payload', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        version: version.value.trim(),
                        sha256: digest.value.trim(),
                        filename: filename.value.trim(),
                    }),
                });
                const body = await resp.json().catch(() => ({}));
                if (!resp.ok) throw new Error(body.error || ('HTTP ' + resp.status));
                cachedPayload = body.payload;
                version.value = digest.value = filename.value = '';
                render();
            } catch (e) {
                msg.textContent = e.message;
                msg.className = 'turn-secret__msg turn-secret__msg--err';
                save.disabled = false;
            }
        });

        card.appendChild(el('label', { class: 'setting__label',
                                       text: t('settings.remote.vdd_version') }));
        card.appendChild(version);
        card.appendChild(el('label', { class: 'setting__label',
                                       text: t('settings.remote.vdd_sha') }));
        card.appendChild(digest);
        card.appendChild(el('label', { class: 'setting__label',
                                       text: t('settings.remote.vdd_filename') }));
        card.appendChild(filename);
        card.appendChild(el('div', { class: 'card-actions' }, [save]));
        card.appendChild(msg);
        return card;
    }

    function render() {
        const panel = document.getElementById(PANEL_ID);
        if (!panel) return;
        for (const id of [CARD_ID, VDD_CARD_ID]) {
            const existing = document.getElementById(id);
            if (existing) existing.remove();
        }
        // Inserted in reverse so TURN status ends up first: it is what an operator checks when a
        // session will not connect, which is far more often than they pin a driver.
        panel.insertBefore(buildVddCard(), panel.firstChild);
        panel.insertBefore(buildCard(), panel.firstChild);
    }

    async function refresh() {
        try {
            const resp = await fetch('/api/remote/turn/status');
            if (!resp.ok) return;            // page already requires manage_settings; bail quietly
            cachedStatus = await resp.json();
        } catch (e) { /* leave cachedStatus as-is */ }
        try {
            const resp = await fetch('/api/remote/virtual-display/payload');
            if (resp.ok) cachedPayload = (await resp.json()).payload;
        } catch (e) { /* the card just shows "None" */ }
        render();
    }

    // Re-inject after settings.js (re)renders the panel; refresh once on initial load.
    document.addEventListener('settings:rendered', render);
    document.addEventListener('DOMContentLoaded', refresh);
})();
