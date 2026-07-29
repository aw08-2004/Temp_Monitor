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
        card.appendChild(el('h3', { class: 'perm-subhead', text: 'Sign-in providers' }));
        card.appendChild(el('p', { class: 'setting__help', text:
            'Only the break-glass administrators listed in ALLOWED_EMAILS can change these. '
            + 'Whoever configures the identity provider decides who this hub believes you '
            + 'are, so it is deliberately not delegable through a permission group.' }));
        return card;
    }

    function buildCard() {
        const card = el('div', { class: 'card', id: CARD_ID });
        card.appendChild(el('h3', { class: 'perm-subhead', text: 'Sign-in providers' }));

        const state = el('div', { class: 'turn-stat' }, [
            el('span', { class: 'turn-stat__label', text: 'Enabled now' }),
            el('span', { class: 'turn-stat__value' }, [
                cached.google_enabled ? pill('Google', 'ok') : null,
                cached.oidc_enabled
                    ? pill(cached.oidc_display_name || 'OIDC', 'ok') : null,
                (!cached.google_enabled && !cached.oidc_enabled)
                    ? pill('None', 'danger') : null,
            ]),
        ]);
        card.appendChild(state);

        card.appendChild(el('p', { class: 'setting__help', text:
            'These are written to the hub\'s .env and applied immediately — no restart. '
            + 'Client secrets are never shown again once saved.' }));

        // ---- Google
        card.appendChild(el('h3', { class: 'perm-subhead', text: 'Google' }));
        card.appendChild(el('p', { class: 'setting__help', text:
            'Create an OAuth 2.0 Client ID (Web application) in the Google Cloud Console '
            + 'and add this hub\'s /auth/callback as an authorised redirect URI. '
            + 'Leave both fields empty to turn Google sign-in off.' }));
        card.appendChild(field('auth-google-id', 'Client ID', cached.google_client_id,
            'Ends in .apps.googleusercontent.com'));
        card.appendChild(field('auth-google-secret', 'Client secret',
            cached.google_client_secret_set ? cached.unchanged_placeholder : '',
            cached.google_client_secret_set
                ? 'A secret is saved. Leave this untouched to keep it, or type a new one to replace it.'
                : 'No secret saved yet.',
            { password: true }));

        // ---- OIDC
        card.appendChild(el('h3', { class: 'perm-subhead', text: 'OIDC provider' }));
        card.appendChild(el('p', { class: 'setting__help', text:
            'Microsoft Entra ID, Okta, Authentik, Keycloak, Auth0 — any OIDC issuer. '
            + 'Register a confidential web application with this hub\'s /auth/oidc/callback '
            + 'as the redirect URI. Endpoints and signing keys come from discovery, so there '
            + 'is nothing vendor-specific to configure here.' }));
        card.appendChild(field('auth-oidc-issuer', 'Issuer URL', cached.oidc_issuer,
            'e.g. https://login.microsoftonline.com/<tenant-id>/v2.0 — the discovery URL is '
            + 'derived from this. Must be https.'));
        card.appendChild(field('auth-oidc-metadata', 'Discovery URL (optional)',
            cached.oidc_metadata_url,
            'Only needed if your provider does not serve /.well-known/openid-configuration '
            + 'under the issuer. Clear it to have it re-derived from the issuer above.'));
        card.appendChild(field('auth-oidc-id', 'Client ID', cached.oidc_client_id, ''));
        card.appendChild(field('auth-oidc-secret', 'Client secret',
            cached.oidc_client_secret_set ? cached.unchanged_placeholder : '',
            cached.oidc_client_secret_set
                ? 'A secret is saved. Leave this untouched to keep it, or type a new one to replace it.'
                : 'No secret saved yet.',
            { password: true }));
        card.appendChild(field('auth-oidc-name', 'Button label', cached.oidc_display_name,
            'What the sign-in button says. "Microsoft" reads far better than "SSO" to '
            + 'somebody looking at an unfamiliar login page.'));
        card.appendChild(field('auth-oidc-scopes', 'Scopes', cached.oidc_scopes,
            'Must include openid. Add "groups" (or your provider\'s equivalent) if you map '
            + 'directory groups to permission groups.'));

        // ---- Save
        const actions = el('div', { class: 'chip-add', style: 'margin-top: var(--space-4);' });
        const button = el('button', { class: 'btn btn--primary', type: 'button',
                                      text: 'Save sign-in settings' });
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
                note.textContent = 'That would leave no way to sign in to this hub. '
                    + 'Configure Google or an OIDC provider before saving.';
                return;
            }
            if (!window.confirm(
                'Change how people sign in to this hub?\n\n'
                + 'This takes effect immediately for new sign-ins. Existing sessions are '
                + 'not signed out. If the new settings are wrong, the break-glass '
                + 'administrators in ALLOWED_EMAILS can change them back — provided they '
                + 'can still sign in.')) return;

            button.disabled = true;
            note.className = 'setting__help';
            note.textContent = 'Applying…';
            try {
                cached = await save(payload);
                note.textContent = cached.changed && cached.changed.length
                    ? 'Saved. Updated: ' + cached.changed.join(', ')
                    : 'No changes to save.';
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
