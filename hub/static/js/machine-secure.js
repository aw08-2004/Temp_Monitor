// The machine page's Lock and wipe card (roadmap #23 phase H).
//
// **The only irreversible thing in this console, so the friction is the feature.** The card is
// closed, the wipe form is behind a second button inside it, and Erase stays disabled until the
// machine's name has been typed out exactly. None of that is decoration: the accident this
// prevents is somebody erasing the device next to the one they meant, and every step is one
// more moment in which the name in front of them has to match the name they typed.
//
// **The typed name is checked on the SERVER as well** (wipe.confirm_wipe). Everything here is a
// courtesy to the operator; nothing here is the control. A confirmation that lives only in
// JavaScript is a confirmation a script does not have.
//
// **Locking asks for none of that.** A lock is undone by the person holding the device with
// their own PIN, and the case it exists for -- a phone left in a taxi -- is one where seconds
// matter. Spreading the friction over both is how people learn to type past it.
//
// The card is hidden unless the device has REPORTED that it can lock, which is the opposite of
// the absent-report rule the hub uses for refusing work. See the note in machine.html.
(function () {
    'use strict';

    const fold = document.getElementById('card-secure');
    if (!fold || !window.MachineContext) return;

    const machine = window.MachineContext.current();
    if (!machine) return;

    const lockButton = document.getElementById('secure-lock');
    const wipeButton = document.getElementById('secure-wipe');
    const confirmEl = document.getElementById('secure-confirm');
    const frpEl = document.getElementById('secure-frp');
    const frpHelpEl = document.getElementById('secure-frp-help');
    const statusEl = document.getElementById('secure-status');
    const errorEl = document.getElementById('secure-error');
    const historyEl = document.getElementById('secure-history');
    const countEl = document.getElementById('secure-count');
    const revealButton = document.getElementById('secure-wipe-reveal');
    const wipePanel = document.getElementById('secure-wipe-panel');

    let canWipe = false;

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    /** Exactly, and case-sensitively, matching wipe.confirm_wipe. A lenient match here would
     *  enable a button the server then refuses, which teaches an operator that the
     *  confirmation is noise. */
    function confirmed() {
        return (confirmEl.value || '') === machine;
    }

    function refreshWipeButton() {
        wipeButton.disabled = !(canWipe && confirmed());
    }

    /** Said in words rather than left to a checkbox label, because the two outcomes are
     *  genuinely different devices afterwards and only one of them can be set up again by the
     *  organisation that owns it. */
    function refreshProtectionHelp() {
        frpHelpEl.textContent = frpEl.checked
            ? t('secure.clear_protection_help')
            : t('secure.keep_protection_help');
    }

    function renderHistory(rows) {
        historyEl.replaceChildren();
        countEl.textContent = rows.length ? t('secure.count', { count: rows.length }) : '';
        if (rows.length === 0) {
            historyEl.appendChild(el('p', 'stat-card__meta', t('secure.no_history')));
            return;
        }
        const table = el('table', 'data-table');
        const head = el('thead');
        const headRow = el('tr');
        [t('secure.column.action'), t('secure.column.who'), t('secure.column.when')]
            .forEach((label) => headRow.appendChild(el('th', null, label)));
        head.appendChild(headRow);
        table.appendChild(head);

        const body = el('tbody');
        rows.forEach((row) => {
            const tr = el('tr');
            // A switch of literal keys, not a computed one: tests/test_i18n.py scans literals.
            tr.appendChild(el('td', null,
                row.action === 'wipe' ? t('secure.action.wipe') : t('secure.action.lock')));
            tr.appendChild(el('td', null, row.requested_by || ''));
            tr.appendChild(el('td', null,
                new Date(row.requested_at * 1000).toLocaleString()));
            body.appendChild(tr);
        });
        table.appendChild(body);
        historyEl.appendChild(table);
    }

    function render(data) {
        canWipe = Boolean(data.can_wipe);
        // A reader sees the history and neither button. The routes re-decide either way; this
        // is so somebody without the capability is not offered something that will refuse.
        lockButton.hidden = !canWipe;
        revealButton.hidden = !canWipe;
        if (!canWipe) wipePanel.hidden = true;
        renderHistory(data.history || []);
        // The one fact this card exists to carry for a device that will never report again: a
        // machine that was wiped is not a machine that went offline, and nothing else in the
        // console can tell an operator which it was.
        if (data.last_wipe) {
            statusEl.textContent = t('secure.wiped_on', {
                when: new Date(data.last_wipe.requested_at * 1000).toLocaleString(),
                who: data.last_wipe.requested_by || '',
            });
        }
        refreshWipeButton();
        refreshProtectionHelp();
    }

    async function post(url, body) {
        errorEl.textContent = '';
        let response;
        try {
            response = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body || {}),
            });
        } catch (e) {
            errorEl.textContent = t('secure.unreachable');
            return null;
        }
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            // Verbatim: wipe.py names the specific refusal -- the typed name did not match,
            // this device cannot be wiped -- and a generic message would throw away the only
            // part that helps.
            errorEl.textContent = data.error || t('secure.failed');
            return null;
        }
        return data;
    }

    async function lock() {
        const data = await post(`/api/wipe/machines/${encodeURIComponent(machine)}/lock`);
        if (!data) return;
        statusEl.textContent = t('secure.lock_sent');
        render(data);
    }

    async function wipe() {
        if (!confirmed()) return;
        // A second, plain confirmation on top of the typed name. Redundant on purpose: this is
        // the last moment before a device stops existing, and the browser's own dialog is the
        // one thing on the page that cannot be clicked through by accident.
        if (!window.confirm(t('secure.confirm_dialog', { machine: machine }))) return;

        const data = await post(`/api/wipe/machines/${encodeURIComponent(machine)}/wipe`, {
            confirm: confirmEl.value,
            reset_protection: frpEl.checked,
        });
        if (!data) return;
        confirmEl.value = '';
        statusEl.textContent = t('secure.wipe_sent');
        render(data);
    }

    async function load() {
        // Whether this device can lock at all, from its own capability report. A machine that
        // has reported nothing is left without the card -- see machine.html.
        let supported = [];
        try {
            const response = await fetch(`/api/machines/${encodeURIComponent(machine)}`);
            if (response.ok) {
                const detail = await response.json();
                supported = (detail && detail.supported_commands) || [];
            }
        } catch (e) {
            return;
        }
        if (!supported.includes('lock_device')) return;

        let response;
        try {
            response = await fetch(`/api/wipe/machines/${encodeURIComponent(machine)}`);
        } catch (e) {
            return;
        }
        if (!response.ok) return;   // out of scope, which for this operator is "no such card"

        fold.hidden = false;
        render(await response.json());
    }

    lockButton.addEventListener('click', lock);
    revealButton.addEventListener('click', () => {
        // Revealed, never toggled back to a remembered state: the panel starts closed on every
        // page load, on every device. See the note in machine.html.
        wipePanel.hidden = !wipePanel.hidden;
        if (!wipePanel.hidden) confirmEl.focus();
    });
    wipeButton.addEventListener('click', wipe);
    confirmEl.addEventListener('input', refreshWipeButton);
    frpEl.addEventListener('change', refreshProtectionHelp);

    load();
})();
