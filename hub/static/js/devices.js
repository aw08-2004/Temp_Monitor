// Paired devices, the second tab on the Users page (roadmap #11).
//
// Lists every device token on this hub and offers exactly one action: revoke. There is no
// "reveal" and there never can be -- the hub stores only the token's hash, so it does not
// have the value to show. Anything a device can do is decided by the intersection of its
// grant with its owner's live capabilities, which is why removing someone from a
// permission group already disables their devices; revoking is the other half, and it is
// about the CREDENTIAL rather than about access.
//
// Wrapped in an IIFE because users.js shares this page's global scope and already owns
// names like `api` and `el`.
(function () {
    'use strict';

    const host = document.getElementById('devices-host');
    if (!host) return;

    let loaded = false;


    function when(epoch) {
        if (!epoch) return t('common.never');
        return new Date(epoch * 1000).toLocaleString();
    }

    function capabilityLabels(capabilities) {
        return (capabilities || [])
            .map((c) => t(`permissions.capability.${c}.label`))
            .join(', ');
    }

    async function revoke(device, row) {
        if (!window.confirm(t('devices.confirm_revoke', {
            device: device.device_name || device.email,
        }))) return;

        try {
            const resp = await fetch(`/api/tokens/${encodeURIComponent(device.token_id)}`, {
                method: 'DELETE',
            });
            let body = null;
            try { body = await resp.json(); } catch (e) { /* empty body is fine */ }
            if (!resp.ok) throw new Error((body && body.error) || `HTTP ${resp.status}`);
            load();
        } catch (err) {
            const cell = row.querySelector('[data-role="status"]');
            if (cell) cell.textContent = err.message;
        }
    }

    function render(devices) {
        host.replaceChildren();
        if (!devices.length) {
            const empty = el('div', 'empty-state');
            empty.appendChild(el('p', null, t('devices.empty')));
            empty.appendChild(el('p', 'stat-card__meta', t('devices.empty_hint')));
            host.appendChild(empty);
            return;
        }

        const table = el('table', 'data-table');
        const head = el('thead');
        const headRow = el('tr');
        [t('devices.col.device'), t('devices.col.owner'), t('devices.col.capabilities'),
         t('devices.col.last_used'), t('devices.col.expires'), ''].forEach((label) => {
            headRow.appendChild(el('th', null, label));
        });
        head.appendChild(headRow);
        table.appendChild(head);

        const body = el('tbody');
        devices.forEach((device) => {
            const row = el('tr');
            const name = el('td');
            name.appendChild(el('div', null, device.device_name || t('devices.unnamed')));
            if (device.platform) {
                name.appendChild(el('div', 'stat-card__meta', device.platform));
            }
            row.appendChild(name);
            row.appendChild(el('td', null, device.email));
            row.appendChild(el('td', 'stat-card__meta',
                capabilityLabels(device.capabilities)));
            row.appendChild(el('td', null, when(device.last_used_at)));
            row.appendChild(el('td', null, when(device.expires_at)));

            const actions = el('td', 'data-table__actions');
            if (device.revoked) {
                // Kept visible rather than filtered out: "this laptop was revoked in
                // March" is the answer somebody is looking for when they ask why an app
                // stopped working, and a row that vanished cannot give it.
                actions.appendChild(el('span', 'badge', t('devices.revoked')));
            } else {
                const button = el('button', 'btn btn--danger', t('common.delete'));
                button.type = 'button';
                button.addEventListener('click', () => revoke(device, row));
                actions.appendChild(button);
            }
            const status = el('span', 'autosave', '');
            status.setAttribute('data-role', 'status');
            actions.appendChild(status);
            row.appendChild(actions);
            body.appendChild(row);
        });
        table.appendChild(body);
        host.appendChild(table);
    }

    async function load() {
        try {
            const resp = await fetch('/api/tokens/all');
            let body = null;
            try { body = await resp.json(); } catch (e) { /* empty body is fine */ }
            if (!resp.ok) throw new Error((body && body.error) || `HTTP ${resp.status}`);
            render(body.devices || []);
        } catch (err) {
            host.replaceChildren();
            host.appendChild(el('p', 'setting__error', err.message));
        }
    }

    // Loaded when the tab is first shown rather than on page load: the directory is what
    // somebody came here for, and a second fetch on every visit to it buys nothing.
    document.getElementById('devices-pane')
        .addEventListener('tab:shown', () => {
            if (loaded) return;
            loaded = true;
            load();
        });
})();
