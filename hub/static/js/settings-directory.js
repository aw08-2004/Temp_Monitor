// Active Directory sync status + "Sync now", for the Settings -> Active Directory tab
// (roadmap #4).
//
// The directory.* fields themselves are schema-rendered by settings.js like every other
// section. This adds the two things a schema field cannot express:
//
//   * whether the sync is actually WORKING -- when it last succeeded, what it found, and
//     the reason if the last attempt failed. A sync that quietly stopped working looks
//     exactly like an AD nobody has changed, so "last succeeded 9 days ago" is the single
//     most useful thing this page can say.
//   * a "Sync now" button. Without it, "did I get the bind DN right?" takes up to an hour
//     to answer, per attempt, which makes the form above effectively untestable.
//
// It re-injects on the 'settings:rendered' event because settings.js replaces the panel's
// children on load and after every save, which would otherwise wipe the card.
(function () {
    'use strict';

    const PANEL_ID = 'tab-directory';
    const CARD_ID = 'directory-status-card';
    let cachedStatus = null;
    let lastResult = null;      // the outcome of a "Sync now" pressed this session

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

    function statRow(label, valueNode) {
        return el('div', { class: 'turn-stat' }, [
            el('span', { class: 'turn-stat__label', text: label }),
            el('span', { class: 'turn-stat__value' }, [valueNode]),
        ]);
    }

    function ago(epochSeconds) {
        if (!epochSeconds) return 'never';
        const secs = Math.max(0, Math.floor(Date.now() / 1000 - epochSeconds));
        if (secs < 90) return 'just now';
        if (secs < 5400) return Math.round(secs / 60) + ' minutes ago';
        if (secs < 172800) return Math.round(secs / 3600) + ' hours ago';
        return Math.round(secs / 86400) + ' days ago';
    }

    async function fetchStatus() {
        const resp = await fetch('/api/directory/status');
        if (!resp.ok) return null;
        return resp.json().catch(() => null);
    }

    async function syncNow() {
        const resp = await fetch('/api/directory/sync', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: '{}',
        });
        const body = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(body.error || ('HTTP ' + resp.status));
        return body;
    }

    function healthPill(status) {
        if (!status.enabled) return pill('Off', 'muted');
        if (!status.library_installed) return pill('Library missing', 'danger');
        const last = status.last_run;
        if (!last) return pill('Never run', 'warn');
        if (last.status === 'failed') return pill('Failing', 'danger');
        return pill('OK', 'ok');
    }

    function buildCard(status) {
        const card = el('div', { class: 'card', id: CARD_ID });
        card.appendChild(el('h3', { class: 'perm-subhead', text: 'Sync status' }));

        card.appendChild(statRow('State', healthPill(status)));

        // Named explicitly rather than left to a generic failure message: "you enabled it
        // but the library isn't installed" is a one-command fix, and it is otherwise
        // indistinguishable from a bad bind DN.
        if (status.enabled && !status.library_installed) {
            card.appendChild(el('p', { class: 'setting__error', text: status.library_hint }));
        }

        // Same reasoning for the credential: the console can say whether it is set
        // without ever being able to read it.
        if (status.enabled && !status.bind_password_set) {
            card.appendChild(el('p', { class: 'setting__error', text:
                'No bind password is set. Add ' + status.bind_password_env + '=... to the '
                + "hub's .env and restart -- it is a credential, so it is deliberately not "
                + 'stored in hub settings.' }));
        }

        const success = status.last_success;
        card.appendChild(statRow('Last successful sync', el('span', {
            text: success ? ago(success.finished_at || success.started_at) : 'never',
        })));
        if (success) {
            card.appendChild(statRow('Found in AD', el('span', {
                text: (success.objects_found || 0) + ' computer objects',
            })));
            card.appendChild(statRow('Matched to machines', el('span', {
                text: String(success.matched || 0),
            })));
            card.appendChild(statRow('Managed but not in AD', el('span', {
                text: String(success.unmatched || 0),
            })));
        }
        card.appendChild(statRow('OUs in use', el('span', {
            text: String(status.ou_count || 0),
        })));

        // The last ATTEMPT, shown only when it is not the last success -- i.e. only when
        // something is currently broken. Otherwise it is a duplicate row.
        const last = status.last_run;
        if (last && last.status === 'failed') {
            card.appendChild(statRow('Last attempt',
                el('span', { text: ago(last.finished_at || last.started_at) + ' — failed' })));
            card.appendChild(el('p', { class: 'setting__error', text: last.error || '' }));
        }

        const actions = el('div', { class: 'chip-add', style: 'margin-top: var(--space-3);' });
        const button = el('button', { class: 'btn btn--primary', type: 'button',
                                      text: 'Sync now' });
        const note = el('span', { class: 'setting__help' });
        if (!status.enabled) {
            button.disabled = true;
            note.textContent = 'Turn the sync on above to run a pass.';
        }
        button.addEventListener('click', async () => {
            button.disabled = true;
            note.className = 'setting__help';
            note.textContent = 'Contacting the domain controller…';
            try {
                lastResult = await syncNow();
                note.textContent = `Synced: ${lastResult.objects_found} objects in AD, `
                    + `${lastResult.matched} machines matched, `
                    + `${lastResult.unmatched.length} not found in AD.`;
                cachedStatus = await fetchStatus();
                inject();
                return;
            } catch (e) {
                note.className = 'setting__error';
                note.textContent = e.message;
            } finally {
                button.disabled = !cachedStatus || !cachedStatus.enabled;
            }
        });
        actions.append(button, note);
        card.appendChild(actions);

        if (lastResult && lastResult.unmatched && lastResult.unmatched.length) {
            card.appendChild(el('p', { class: 'setting__help', text:
                'Not found in AD: ' + lastResult.unmatched.slice(0, 20).join(', ')
                + (lastResult.unmatched.length > 20 ? '…' : '') }));
        }
        if (lastResult && lastResult.duplicates && lastResult.duplicates.length) {
            card.appendChild(el('p', { class: 'setting__help', text:
                'These hostnames have more than one computer object in AD; the first of '
                + 'each was used: ' + lastResult.duplicates.join(', ') }));
        }
        return card;
    }

    function inject() {
        const panel = document.getElementById(PANEL_ID);
        if (!panel || !cachedStatus) return;
        const existing = document.getElementById(CARD_ID);
        if (existing) existing.remove();
        // Prepended: the status is what you look at first, and the fields below are what
        // you change in response to it.
        panel.insertBefore(buildCard(cachedStatus), panel.firstChild);
    }

    async function refresh() {
        cachedStatus = await fetchStatus();
        inject();
    }

    document.addEventListener('settings:rendered', inject);
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', refresh);
    } else {
        refresh();
    }
})();
