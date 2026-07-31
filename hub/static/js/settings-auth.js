// Sign-in provider editor for the Settings -> Sign-in tab.
//
// Unlike every other settings panel, this one is built entirely here: its values live in
// .env rather than the settings table (they are credentials, and the perimeter itself), so
// there is no schema for settings.js to render.
//
// Three rules the UI follows, each mirroring a rule the server enforces:
//
//   * a secret that is set renders as a placeholder and is sent back unchanged unless the
//     admin actually types a new one. The server treats that placeholder as "leave it
//     alone", so a saved form never blanks a secret nobody looked at.
//   * the Save button refuses a configuration with no provider enabled, before asking the
//     server. Not a substitute for the server check -- it is the one mistake whose
//     consequence is nobody being able to sign in again, so it is worth catching twice.
//   * changing the perimeter asks for confirmation, and says what will happen.
//
// It re-injects on 'settings:rendered' like the other panel scripts, because settings.js
// replaces panel children on load and after every save.
(function () {
    'use strict';

    const PANEL_ID = 'tab-signin';
    const CARD_ID = 'signin-config-card';
    let cached = null;        // the redacted config from GET /api/auth/providers
    let denied = false;       // not a break-glass admin

    function el(tag, props, children) {
        const node = document.createElement(tag);
        if (props) {
            for (const [k, v] of Object.entries(props)) {
                if (k === 'class') node.className = v;
                else if (k === 'text') node.textContent = v;
                else if (k === 'value') node.value = v;
                else node.setAttribute(k, v);
            }
        }
        for (const child of children || []) {
            if (child) node.appendChild(typeof child === 'string'
                ? document.createTextNode(child) : child);
        }
        return node;
    }

    function pill(text, kind) {
        return el('span', { class: 'status-pill status-pill--' + kind }, [
            el('span', { class: 'status-pill__dot' }), text,
        ]);
    }

    function field(id, label, value, help, opts) {
        const options = opts || {};
        const wrap = el('div', { class: 'setting' });
        wrap.appendChild(el('label', { class: 'setting__label', for: id, text: label }));
        const input = el('input', {
            class: 'input', id: id, autocomplete: 'off',
            type: options.password ? 'password' : 'text',
            value: value || '',
        });
        if (options.placeholder) input.placeholder = options.placeholder;
        input.style.width = '100%';
        wrap.appendChild(input);
        if (help) wrap.appendChild(el('p', { class: 'setting__help', text: help }));
        return wrap;
    }

    function val(id) {
        const node = document.getElementById(id);
        return node ? node.value.trim() : '';
    }

    async function load() {
        const resp = await fetch('/api/auth/providers');
        if (resp.status === 403) { denied = true; return null; }
        if (!resp.ok) return null;
        return resp.json().catch(() => null);
    }

    async function save(payload) {
        const resp = await fetch('/api/auth/providers', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const body = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(body.error || ('HTTP ' + resp.status));
        return body;
    }

    function deniedCard() {
        const card = el('div', { class: 'card', id: CARD_ID });
        card.appendChild(el('h3', { class: 'perm-subhead',
                                   text: t('settings.signin.providers') }));
        card.appendChild(el('p', { class: 'setting__help',
                                   text: t('settings.signin.denied') }));
        return card;
    }

    function buildCard() {
        const card = el('div', { class: 'card', id: CARD_ID });
        card.appendChild(el('h3', { class: 'perm-subhead',
                                   text: t('settings.signin.providers') }));

        const state = el('div', { class: 'turn-stat' }, [
            el('span', { class: 'turn-stat__label', text: t('settings.signin.enabled_now') }),
            el('span', { class: 'turn-stat__value' }, [
                cached.google_enabled ? pill('Google', 'ok') : null,
                cached.oidc_enabled
                    ? pill(cached.oidc_display_name || 'OIDC', 'ok') : null,
                (!cached.google_enabled && !cached.oidc_enabled)
                    ? pill(t('settings.signin.none'), 'danger') : null,
            ]),
        ]);
        card.appendChild(state);

        card.appendChild(el('p', { class: 'setting__help',
                                   text: t('settings.signin.env_note') }));

        // ---- Google
        card.appendChild(el('h3', { class: 'perm-subhead', text: t('settings.signin.google') }));
        card.appendChild(el('p', { class: 'setting__help',
                                   text: t('settings.signin.google_help') }));
        card.appendChild(field('auth-google-id', t('settings.signin.client_id'),
            cached.google_client_id, t('settings.signin.google_id_help')));
        card.appendChild(field('auth-google-secret', t('settings.signin.client_secret'),
            cached.google_client_secret_set ? cached.unchanged_placeholder : '',
            cached.google_client_secret_set ? t('settings.signin.secret_saved')
                                            : t('settings.signin.secret_unsaved'),
            { password: true }));

        // ---- OIDC
        card.appendChild(el('h3', { class: 'perm-subhead', text: t('settings.signin.oidc') }));
        card.appendChild(el('p', { class: 'setting__help',
                                   text: t('settings.signin.oidc_help') }));
        card.appendChild(field('auth-oidc-issuer', t('settings.signin.issuer'),
            cached.oidc_issuer, t('settings.signin.issuer_help')));
        card.appendChild(field('auth-oidc-metadata', t('settings.signin.metadata'),
            cached.oidc_metadata_url, t('settings.signin.metadata_help')));
        card.appendChild(field('auth-oidc-id', t('settings.signin.client_id'),
            cached.oidc_client_id, ''));
        card.appendChild(field('auth-oidc-secret', t('settings.signin.client_secret'),
            cached.oidc_client_secret_set ? cached.unchanged_placeholder : '',
            cached.oidc_client_secret_set ? t('settings.signin.secret_saved')
                                          : t('settings.signin.secret_unsaved'),
            { password: true }));
        card.appendChild(field('auth-oidc-name', t('settings.signin.button_label'),
            cached.oidc_display_name, t('settings.signin.button_label_help')));
        card.appendChild(field('auth-oidc-scopes', t('settings.signin.scopes'),
            cached.oidc_scopes, t('settings.signin.scopes_help')));

        // ---- Save
        const actions = el('div', { class: 'chip-add', style: 'margin-top: var(--space-4);' });
        const button = el('button', { class: 'btn btn--primary', type: 'button',
                                      text: t('settings.signin.save') });
        const note = el('span', { class: 'setting__help' });
        button.addEventListener('click', async () => {
            const payload = {
                google_client_id: val('auth-google-id'),
                google_client_secret: val('auth-google-secret'),
                oidc_client_id: val('auth-oidc-id'),
                oidc_client_secret: val('auth-oidc-secret'),
                oidc_issuer: val('auth-oidc-issuer'),
                oidc_metadata_url: val('auth-oidc-metadata'),
                oidc_display_name: val('auth-oidc-name'),
                oidc_scopes: val('auth-oidc-scopes'),
            };
            // Mirrors the server's refusal. Checked here too because this is the one
            // mistake whose consequence is that nobody can sign in to fix it.
            const googleOn = payload.google_client_id && payload.google_client_secret;
            const oidcOn = payload.oidc_client_id && payload.oidc_client_secret
                && (payload.oidc_issuer || payload.oidc_metadata_url);
            if (!googleOn && !oidcOn) {
                note.className = 'setting__error';
                note.textContent = t('settings.signin.no_provider_left');
                return;
            }
            if (!window.confirm(t('settings.signin.confirm'))) return;

            button.disabled = true;
            note.className = 'setting__help';
            note.textContent = t('settings.signin.applying');
            try {
                cached = await save(payload);
                note.textContent = cached.changed && cached.changed.length
                    ? t('settings.signin.saved_changed',
                        { fields: cached.changed.join(', ') })
                    : t('settings.signin.saved_unchanged');
                inject();
                return;
            } catch (e) {
                note.className = 'setting__error';
                note.textContent = e.message;
            } finally {
                button.disabled = false;
            }
        });
        actions.append(button, note);
        card.appendChild(actions);

        if (cached.superusers && cached.superusers.length) {
            card.appendChild(el('p', { class: 'setting__help', text:
                'Break-glass administrators who can change this: '
                + cached.superusers.join(', ') + '. They are set by ALLOWED_EMAILS in .env '
                + 'and can always sign in regardless of permission groups.' }));
        }
        return card;
    }

    function inject() {
        const panel = document.getElementById(PANEL_ID);
        if (!panel) return;
        if (!cached && !denied) return;
        const existing = document.getElementById(CARD_ID);
        if (existing) existing.remove();
        panel.insertBefore(denied ? deniedCard() : buildCard(), panel.firstChild);
    }

    async function refresh() {
        cached = await load();
        inject();
    }

    document.addEventListener('settings:rendered', inject);
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', refresh);
    } else {
        refresh();
    }
})();
