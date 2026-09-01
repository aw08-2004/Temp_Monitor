// Cross-hub machine sharing (roadmap #15): what this hub lends, what it borrows, and which
// hubs it speaks to.
//
// Two rules govern everything in this file, and both are about not lying to the operator:
//
//   1. **A borrowed machine is never dressed up as a local one.** It is badged, it is never
//      linked into the machine page, and when its owner hub stops answering it says so with
//      the time it was last heard from rather than quietly showing a stale row as current.
//      The console must never let "somebody else's machine, seen through a window" blur into
//      "one of ours".
//   2. **Every capability check here is presentation.** Which buttons get drawn comes from
//      the borrowed cache, which is one hub's opinion of another hub's grant. The OWNING hub
//      re-decides every request against rows this one cannot see. So a refusal from over
//      there is rendered verbatim, never swallowed and never second-guessed -- it is the
//      only answer that counts.
(function () {
    'use strict';

    const shareHost = document.getElementById('shares-host');
    if (!shareHost) return;

    const borrowedHost = document.getElementById('borrowed-host');
    const borrowedDetail = document.getElementById('borrowed-detail');
    const peersHost = document.getElementById('peers-host');
    const linksHost = document.getElementById('links-host');
    const disabledBanner = document.getElementById('sharing-disabled');

    let peers = [];
    let shareable = [];
    let defaults = [];
    let editing = null;

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function when(epoch) {
        if (!epoch) return t('common.never');
        return new Date(epoch * 1000).toLocaleString();
    }

    function capabilityLabels(capabilities) {
        return (capabilities || [])
            .map((c) => t(`permissions.capability.${c}.label`))
            .join(', ');
    }

    async function api(url, options) {
        const resp = await fetch(url, options);
        let body = null;
        try { body = await resp.json(); } catch (e) { /* an empty body is fine */ }
        if (!resp.ok) throw new Error((body && body.error) || `HTTP ${resp.status}`);
        return body;
    }

    function emptyState(host, message, hint) {
        const empty = el('div', 'empty-state');
        empty.appendChild(el('p', null, message));
        if (hint) empty.appendChild(el('p', 'stat-card__meta', hint));
        host.replaceChildren(empty);
    }

    function table(host, columns) {
        const node = el('table', 'data-table');
        const head = el('thead');
        const row = el('tr');
        columns.forEach((label) => row.appendChild(el('th', null, label)));
        head.appendChild(row);
        node.appendChild(head);
        const body = el('tbody');
        node.appendChild(body);
        host.replaceChildren(node);
        return body;
    }

    function actionCell(buttons) {
        const cell = el('td');
        cell.style.whiteSpace = 'nowrap';
        buttons.forEach((button) => cell.appendChild(button));
        return cell;
    }

    function button(label, className, onClick) {
        const node = el('button', className || 'btn', label);
        node.type = 'button';
        node.style.marginLeft = 'var(--space-2)';
        node.addEventListener('click', onClick);
        return node;
    }

    // ================================
    // WHAT WE LEND
    // ================================
    function shareState(share) {
        // Three distinct answers, and collapsing them would hide the one that needs acting
        // on. Expired is a decision somebody made; suspended is an ACCIDENT -- the operator
        // who lent the machine has lost a permission since -- and it is the only one of the
        // three that somebody has to go and fix.
        if (share.expired) return { label: t('sharing.state.expired'), tone: 'muted' };
        if (share.lapsed && share.lapsed.length) {
            return {
                label: t('sharing.state.suspended'),
                tone: 'warn',
                detail: t('sharing.state.suspended_detail', {
                    operator: share.created_by,
                    capabilities: capabilityLabels(share.lapsed),
                }),
            };
        }
        return { label: t('sharing.state.live'), tone: 'ok' };
    }

    function renderShares(shares) {
        if (!shares.length) {
            emptyState(shareHost, t('sharing.lending.empty'),
                       t('sharing.lending.empty_hint'));
            return;
        }
        const body = table(shareHost, [
            t('sharing.col.machine'), t('sharing.col.peer'),
            t('sharing.col.capabilities'), t('sharing.col.expires'),
            t('sharing.col.state'), '']);

        shares.forEach((share) => {
            const row = el('tr');
            row.appendChild(el('td', null, share.machine));
            row.appendChild(el('td', null, share.peer_label || t('sharing.unnamed_hub')));
            row.appendChild(el('td', 'stat-card__meta',
                               capabilityLabels(share.capabilities)));
            row.appendChild(el('td', null,
                               share.expires_at ? when(share.expires_at)
                                                : t('sharing.no_expiry')));

            const state = shareState(share);
            const stateCell = el('td');
            const pill = el('span', `status-pill status-pill--${state.tone}`);
            pill.appendChild(el('span', 'status-pill__dot'));
            pill.appendChild(el('span', null, state.label));
            stateCell.appendChild(pill);
            if (state.detail) {
                stateCell.appendChild(el('div', 'stat-card__meta', state.detail));
            }
            row.appendChild(stateCell);

            row.appendChild(actionCell([
                button(t('common.edit'), 'btn', () => openShare(share)),
                button(t('sharing.revoke'), 'btn btn--danger', () => revokeShare(share)),
            ]));
            body.appendChild(row);
        });
    }

    async function revokeShare(share) {
        if (!window.confirm(t('sharing.confirm_revoke', {
            machine: share.machine,
            hub: share.peer_label || t('sharing.unnamed_hub'),
        }))) return;
        try {
            await api(`/api/sharing/shares/${encodeURIComponent(share.share_id)}`,
                      { method: 'DELETE' });
            loadShares();
        } catch (err) {
            window.alert(err.message);
        }
    }

    // ---------------- the share dialog ----------------
    const shareModal = document.getElementById('share-modal');
    const shareError = document.getElementById('share-error');
    const capabilityHost = document.getElementById('share-capabilities');

    function checkedCapabilities() {
        return Array.from(capabilityHost.querySelectorAll('input:checked'))
            .map((input) => input.value);
    }

    function renderCapabilityChoices(selected) {
        capabilityHost.replaceChildren();
        shareable.forEach((name) => {
            const label = el('label', 'chip');
            const input = document.createElement('input');
            input.type = 'checkbox';
            input.value = name;
            input.checked = selected.includes(name);
            label.appendChild(input);
            label.appendChild(el('span', null,
                                 t(`permissions.capability.${name}.label`)));
            label.title = t(`permissions.capability.${name}.description`);
            capabilityHost.appendChild(label);
        });
    }

    function localDatetimeValue(epoch) {
        if (!epoch) return '';
        const date = new Date(epoch * 1000);
        // datetime-local wants local wall-clock with no zone, and toISOString is UTC --
        // subtracting the offset first is what keeps "17:00" meaning 17:00 here.
        const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
        return local.toISOString().slice(0, 16);
    }

    function openShare(share) {
        editing = share || null;
        shareError.textContent = '';

        const peerSelect = document.getElementById('share-peer');
        peerSelect.replaceChildren();
        peers.forEach((peer) => {
            const option = document.createElement('option');
            option.value = peer.peer_id;
            option.textContent = peer.label || peer.peer_label || peer.peer_id;
            peerSelect.appendChild(option);
        });
        // A peer cannot be moved after the fact: a share is a grant TO a named hub, and
        // re-aiming one would be a new decision wearing the old one's audit trail.
        peerSelect.disabled = Boolean(editing);
        if (editing) peerSelect.value = editing.peer_id;

        const machineInput = document.getElementById('share-machine');
        machineInput.value = editing ? editing.machine : '';
        machineInput.disabled = Boolean(editing);

        renderCapabilityChoices(editing ? editing.capabilities : defaults);
        document.getElementById('share-expires').value =
            localDatetimeValue(editing && editing.expires_at);
        document.getElementById('share-modal-title').textContent =
            editing ? t('sharing.lending.edit') : t('sharing.lending.add');
        shareModal.showModal();
    }

    async function saveShare() {
        const expiresRaw = document.getElementById('share-expires').value;
        const payload = {
            capabilities: checkedCapabilities(),
            expires_at: expiresRaw ? Math.floor(new Date(expiresRaw).getTime() / 1000)
                                   : null,
        };
        try {
            if (editing) {
                await api(`/api/sharing/shares/${encodeURIComponent(editing.share_id)}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
            } else {
                payload.peer_id = document.getElementById('share-peer').value;
                payload.machine = document.getElementById('share-machine').value.trim();
                await api('/api/sharing/shares', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
            }
            shareModal.close();
            loadShares();
        } catch (err) {
            shareError.textContent = err.message;
        }
    }

    async function loadShares() {
        try {
            const data = await api('/api/sharing/shares');
            shareable = data.shareable || [];
            defaults = data.defaults || [];
            if (disabledBanner) disabledBanner.hidden = data.enabled !== false;
            renderShares(data.shares || []);
        } catch (err) {
            emptyState(shareHost, err.message);
        }
        try {
            // Only an operator who may manage peers can read this, and the rest of the page
            // works without it -- a failure here just means the "share a machine" dialog has
            // no hubs to offer, which it says for itself.
            const data = await api('/api/sharing/peers');
            peers = data.peers || [];
        } catch (err) {
            peers = [];
        }
        const newShare = document.getElementById('new-share');
        if (newShare) newShare.disabled = peers.length === 0;
    }

    async function loadMachineOptions() {
        try {
            const data = await api('/api/machines');
            const list = document.getElementById('share-machine-options');
            list.replaceChildren();
            (data.machines || data || []).forEach((machine) => {
                const option = document.createElement('option');
                option.value = machine.machine || machine;
                list.appendChild(option);
            });
        } catch (err) { /* the field still accepts a typed hostname */ }
    }

    // ================================
    // WHAT WE BORROW
    // ================================
    function renderBorrowed(machines) {
        if (!machines.length) {
            emptyState(borrowedHost, t('sharing.borrowing.empty'),
                       t('sharing.borrowing.empty_hint'));
            if (borrowedDetail) borrowedDetail.hidden = true;
            return;
        }
        const body = table(borrowedHost, [
            t('sharing.col.machine'), t('sharing.col.owner_hub'),
            t('sharing.col.capabilities'), t('sharing.col.state'), '']);

        machines.forEach((machine) => {
            const row = el('tr');
            const name = el('td');
            name.appendChild(el('div', null, machine.hostname));
            // Badged on every row, not once at the top of the table. A borrowed machine is
            // read one row at a time, and a heading above the list is not where somebody
            // looks before clicking a button.
            name.appendChild(el('span', 'badge', t('sharing.borrowed_badge')));
            row.appendChild(name);
            row.appendChild(el('td', null,
                               machine.peer_label || t('sharing.unnamed_hub')));
            row.appendChild(el('td', 'stat-card__meta',
                               machine.capabilities.length
                                   ? capabilityLabels(machine.capabilities)
                                   : t('sharing.nothing_granted')));

            const stateCell = el('td');
            let tone = machine.online ? 'ok' : 'muted';
            let label = machine.online ? t('sharing.state.online')
                                       : t('sharing.state.offline');
            // Stale outranks online/offline, because it is a statement about a DIFFERENT
            // thing: the owner hub has not answered, so this row's idea of online is old
            // and showing it as fact would be the one misreading that matters.
            if (machine.stale) {
                tone = 'warn';
                label = t('sharing.state.unreachable');
            } else if (machine.lapsed) {
                tone = 'warn';
                label = t('sharing.state.suspended');
            }
            const pill = el('span', `status-pill status-pill--${tone}`);
            pill.appendChild(el('span', 'status-pill__dot'));
            pill.appendChild(el('span', null, label));
            stateCell.appendChild(pill);
            if (machine.stale) {
                stateCell.appendChild(el('div', 'stat-card__meta',
                    t('sharing.state.last_heard', { when: when(machine.cached_at) })));
            }
            row.appendChild(stateCell);

            const buttons = [];
            if (machine.capabilities.includes('view')) {
                buttons.push(button(t('sharing.borrowing.open'), 'btn',
                                    () => openBorrowed(machine)));
            }
            row.appendChild(actionCell(buttons));
            body.appendChild(row);
        });
    }

    async function openBorrowed(machine) {
        if (!borrowedDetail) return;
        borrowedDetail.hidden = false;
        borrowedDetail.replaceChildren(el('p', 'stat-card__meta', t('common.loading')));
        try {
            const url = `/api/sharing/borrowed/${encodeURIComponent(machine.link_id)}`
                + `/${encodeURIComponent(machine.share_id)}/machine`;
            renderBorrowedDetail(machine, await api(url));
        } catch (err) {
            // Verbatim. A refusal from the owning hub explains itself -- "the operator who
            // shared this machine no longer holds 'view' on it" -- and rewording it here
            // into something generic would throw away the only actionable half.
            borrowedDetail.replaceChildren(el('div', 'banner banner--warn', err.message));
        }
    }

    function renderBorrowedDetail(machine, detail) {
        const card = el('div', 'card');
        card.style.marginTop = 'var(--space-5)';
        const head = el('div');
        head.appendChild(el('h2', 'section-title', detail.machine || machine.hostname));
        head.appendChild(el('span', 'badge', t('sharing.borrowed_badge')));
        card.appendChild(head);
        card.appendChild(el('p', 'stat-card__meta',
            t('sharing.borrowing.owned_by', {
                hub: machine.peer_label || t('sharing.unnamed_hub'),
            })));

        const facts = [
            [t('sharing.detail.model'),
             [detail.manufacturer, detail.model].filter(Boolean).join(' ')],
            [t('sharing.detail.os'), detail.os_caption || detail.os],
            [t('sharing.detail.status'), detail.status],
            [t('sharing.detail.temp'),
             detail.temp === null || detail.temp === undefined ? null : `${detail.temp} °C`],
            [t('sharing.detail.uptime'),
             detail.uptime_seconds
                 ? t('sharing.detail.days', {
                     days: Math.floor(detail.uptime_seconds / 86400) })
                 : null],
        ];
        // The machine page's own stat cards, so a borrowed machine reads the way a local
        // one does where the facts are genuinely the same fact. What must NOT match is the
        // framing around them -- hence the badge above and the note below.
        const grid = el('div', 'card-grid');
        facts.forEach(([label, value]) => {
            if (!value) return;
            const tile = el('div', 'card stat-card');
            tile.appendChild(el('div', 'stat-card__label', label));
            tile.appendChild(el('div', 'stat-card__value', String(value)));
            grid.appendChild(tile);
        });
        card.appendChild(grid);
        card.appendChild(el('p', 'stat-card__meta', t('sharing.detail.projection_note')));
        borrowedDetail.replaceChildren(card);
    }

    async function loadBorrowed() {
        if (!borrowedHost) return;
        try {
            renderBorrowed((await api('/api/sharing/borrowed')).machines || []);
        } catch (err) {
            emptyState(borrowedHost, err.message);
        }
    }

    async function refreshBorrowed() {
        // Refreshing is per-link, because that is what the API is: one poll of one peer.
        // Doing them in sequence rather than at once keeps a slow peer from being reported
        // as the reason a fast one failed.
        let links = [];
        try {
            links = (await api('/api/sharing/links')).links || [];
        } catch (err) {
            // Without manage_permission_groups this operator cannot enumerate links. The
            // cached list is still theirs to read; it is simply not theirs to refresh.
            await loadBorrowed();
            return;
        }
        for (const link of links) {
            try {
                await api(`/api/sharing/links/${encodeURIComponent(link.link_id)}/refresh`,
                          { method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: '{}' });
            } catch (err) { /* recorded on the link; the list below shows it */ }
        }
        await loadBorrowed();
        await loadLinks();
    }

    // ================================
    // WHICH HUBS
    // ================================
    async function loadPeers() {
        if (!peersHost) return;
        try {
            const data = await api('/api/sharing/peers');
            peers = data.peers || [];
            if (!peers.length) {
                emptyState(peersHost, t('sharing.hubs.no_peers'),
                           t('sharing.hubs.no_peers_hint'));
                return;
            }
            const body = table(peersHost, [
                t('sharing.col.hub'), t('sharing.col.machines_lent'),
                t('sharing.col.last_seen'), t('sharing.col.expires'), '']);
            peers.forEach((peer) => {
                const row = el('tr');
                const name = el('td');
                name.appendChild(el('div', null, peer.label || t('sharing.unnamed_hub')));
                if (peer.peer_label && peer.peer_label !== peer.label) {
                    // What they call themselves, kept visibly apart from what we call them:
                    // one is our note, the other is their claim.
                    name.appendChild(el('div', 'stat-card__meta',
                        t('sharing.hubs.calls_itself', { name: peer.peer_label })));
                }
                row.appendChild(name);
                row.appendChild(el('td', null, String((peer.shares || []).length)));
                row.appendChild(el('td', null, when(peer.last_seen_at)));
                row.appendChild(el('td', null, when(peer.expires_at)));
                row.appendChild(actionCell([
                    button(t('sharing.hubs.unpair'), 'btn btn--danger',
                           () => unpair(peer)),
                ]));
                body.appendChild(row);
            });
        } catch (err) {
            emptyState(peersHost, err.message);
        }
    }

    async function unpair(peer) {
        if (!window.confirm(t('sharing.hubs.confirm_unpair', {
            hub: peer.label || t('sharing.unnamed_hub'),
            count: (peer.shares || []).length,
        }))) return;
        try {
            await api(`/api/sharing/peers/${encodeURIComponent(peer.peer_id)}`,
                      { method: 'DELETE' });
            loadPeers();
            loadShares();
        } catch (err) {
            window.alert(err.message);
        }
    }

    async function loadLinks() {
        if (!linksHost) return;
        try {
            const links = (await api('/api/sharing/links')).links || [];
            if (!links.length) {
                emptyState(linksHost, t('sharing.hubs.no_links'),
                           t('sharing.hubs.no_links_hint'));
                return;
            }
            const body = table(linksHost, [
                t('sharing.col.hub'), t('sharing.col.address'),
                t('sharing.col.machines_borrowed'), t('sharing.col.state'), '']);
            links.forEach((link) => {
                const row = el('tr');
                row.appendChild(el('td', null, link.label || t('sharing.unnamed_hub')));
                row.appendChild(el('td', 'stat-card__meta', link.base_url));
                row.appendChild(el('td', null, String(link.machines || 0)));

                const stateCell = el('td');
                if (!link.has_token) {
                    stateCell.appendChild(el('div', 'stat-card__meta',
                                             t('sharing.hubs.no_token')));
                } else if (link.last_error) {
                    // Both timestamps, deliberately: a peer that works now and broke an hour
                    // ago is an intermittent one, and showing only the newer fact would
                    // hide exactly the case somebody needs to chase.
                    stateCell.appendChild(el('div', null,
                        t('sharing.hubs.last_ok', { when: when(link.last_ok_at) })));
                    stateCell.appendChild(el('div', 'stat-card__meta',
                        t('sharing.hubs.last_error', {
                            when: when(link.last_error_at), error: link.last_error })));
                } else {
                    stateCell.appendChild(el('div', null,
                        t('sharing.hubs.last_ok', { when: when(link.last_ok_at) })));
                }
                row.appendChild(stateCell);

                row.appendChild(actionCell([
                    button(t('sharing.hubs.remove'), 'btn btn--danger',
                           () => removeLink(link)),
                ]));
                body.appendChild(row);
            });
        } catch (err) {
            emptyState(linksHost, err.message);
        }
    }

    async function removeLink(link) {
        // Says plainly that this end cannot unpair the other -- it holds no credential over
        // there. Pretending otherwise would leave an operator believing they had revoked
        // something they had not.
        if (!window.confirm(t('sharing.hubs.confirm_remove', {
            hub: link.label || link.base_url,
        }))) return;
        try {
            await api(`/api/sharing/links/${encodeURIComponent(link.link_id)}`,
                      { method: 'DELETE' });
            loadLinks();
            loadBorrowed();
        } catch (err) {
            window.alert(err.message);
        }
    }

    // ================================
    // WIRING
    // ================================
    const pairingModal = document.getElementById('pairing-modal');
    const linkModal = document.getElementById('link-modal');

    document.getElementById('new-share').addEventListener('click', () => openShare(null));
    document.getElementById('share-cancel')
        .addEventListener('click', () => shareModal.close());
    document.getElementById('share-save').addEventListener('click', saveShare);

    const refreshButton = document.getElementById('refresh-borrowed');
    if (refreshButton) refreshButton.addEventListener('click', refreshBorrowed);

    const newPairing = document.getElementById('new-pairing');
    if (newPairing) {
        newPairing.addEventListener('click', () => {
            document.getElementById('pairing-label').value = '';
            document.getElementById('pairing-result').hidden = true;
            document.getElementById('pairing-error').textContent = '';
            pairingModal.showModal();
        });
        document.getElementById('pairing-close')
            .addEventListener('click', () => { pairingModal.close(); loadPeers(); });
        document.getElementById('pairing-create').addEventListener('click', async () => {
            try {
                const data = await api('/api/sharing/pairings', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        label: document.getElementById('pairing-label').value.trim() }),
                });
                document.getElementById('pairing-code').value = data.code;
                document.getElementById('pairing-expiry').textContent =
                    t('sharing.field.code_expiry',
                      { minutes: Math.round(data.expires_in / 60) });
                document.getElementById('pairing-result').hidden = false;
            } catch (err) {
                document.getElementById('pairing-error').textContent = err.message;
            }
        });
    }

    const newLink = document.getElementById('new-link');
    if (newLink) {
        newLink.addEventListener('click', () => {
            ['link-url', 'link-code', 'link-label'].forEach((id) => {
                document.getElementById(id).value = '';
            });
            document.getElementById('link-error').textContent = '';
            linkModal.showModal();
        });
        document.getElementById('link-cancel')
            .addEventListener('click', () => linkModal.close());
        document.getElementById('link-save').addEventListener('click', async () => {
            try {
                await api('/api/sharing/links', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        base_url: document.getElementById('link-url').value.trim(),
                        code: document.getElementById('link-code').value.trim(),
                        label: document.getElementById('link-label').value.trim(),
                    }),
                });
                linkModal.close();
                loadLinks();
                loadBorrowed();
            } catch (err) {
                document.getElementById('link-error').textContent = err.message;
            }
        });
    }

    loadShares();
    loadMachineOptions();
    loadBorrowed();
    loadPeers();
    loadLinks();
}());
