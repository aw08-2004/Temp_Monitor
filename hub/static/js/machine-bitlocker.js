// The machine page's Drive encryption card (roadmap #19): what protects this PC's volumes,
// and whether anybody still holds the key.
//
// **The card answers "do we have the key" before it answers "what is the key".** That is the
// question worth asking while the machine still works, and it is the one an operator can act
// on -- a volume protected by a TPM alone, with no recovery password escrowed anywhere, is a
// reimage waiting for a firmware update. The password itself is one deliberate click behind
// it, and reading it writes a security-level row into the audit log.
//
// **Three states look alike and are not**, so each gets its own wording: a machine that has
// never reported (the card is absent), a machine that reports it has no BitLocker at all
// (`unsupported`, a permanent and correct answer for Home editions and for every Linux and
// Android device), and a machine whose provider failed. Collapsing the first into the second
// would write off a fleet the day before the agent release lands.
//
// **There is no agent version gate here**, unlike the Files and Processes tabs, and that is
// not an oversight: nothing on this card asks a machine to do anything. It renders stored
// facts and a hub-side reveal, so `support === null` -- "no agent has told us" -- already says
// everything a version number would, and says it correctly for a machine that is simply
// offline. See tests/test_bitlocker.py for the hub half of that distinction.
//
// Nothing here polls. Encryption state changes on the day somebody turns BitLocker on.
(function () {
    'use strict';

    const fold = document.getElementById('card-bitlocker');
    if (!fold || !window.MachineContext) return;

    const machine = window.MachineContext.current();
    if (!machine) return;

    const bodyEl = document.getElementById('bitlocker-body');
    const summaryEl = document.getElementById('bitlocker-summary');
    const noteEl = document.getElementById('bitlocker-note');
    const errorEl = document.getElementById('bitlocker-error');
    const keyEl = document.getElementById('bitlocker-key');
    const keyLabelEl = document.getElementById('bitlocker-key-label');
    const keyValueEl = document.getElementById('bitlocker-key-value');

    let inventory = null;

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    // Literal keys in a switch rather than one built by concatenation: tests/test_i18n.py can
    // only scan for literals, so a computed key whose translation was never written would ship
    // silently and caption the card with itself.
    function protectionLabel(state) {
        if (state === 'on') return t('bitlocker.protection.on');
        if (state === 'off') return t('bitlocker.protection.off');
        return t('bitlocker.protection.unknown');
    }

    function conversionLabel(state) {
        if (state === 'fully_encrypted') return t('bitlocker.conversion.fully_encrypted');
        if (state === 'fully_decrypted') return t('bitlocker.conversion.fully_decrypted');
        if (state === 'encrypting') return t('bitlocker.conversion.encrypting');
        if (state === 'decrypting') return t('bitlocker.conversion.decrypting');
        if (state === 'encryption_paused') return t('bitlocker.conversion.encryption_paused');
        if (state === 'decryption_paused') return t('bitlocker.conversion.decryption_paused');
        return '';
    }

    function volumeName(volume) {
        return volume.mount || volume.device_id;
    }

    // What the hub holds for one volume, as a sentence rather than a count. "1 of 2 escrowed"
    // is a number nobody can act on; "no recovery key is escrowed" is the finding.
    function escrowCell(volume) {
        const recovery = (volume.protectors || [])
            .filter((p) => p.kind === 'recovery_password');
        const held = recovery.filter((p) => p.escrowed);
        const cell = el('td');

        if (recovery.length === 0) {
            // The case the whole feature exists for. A volume with a TPM protector and no
            // recovery password has nothing anybody can type at a recovery screen -- not here,
            // not in AD, not on a printout.
            cell.appendChild(el('span', null, t('bitlocker.escrow.no_recovery_protector')));
            return cell;
        }
        if (held.length === 0) {
            cell.appendChild(el('span', null, inventory.escrow_enabled
                ? t('bitlocker.escrow.pending')
                : t('bitlocker.escrow.disabled')));
            return cell;
        }

        held.forEach((protector) => {
            const row = el('div');
            row.appendChild(el('span', null, t('bitlocker.escrow.held', {
                when: new Date(protector.escrowed_at * 1000).toLocaleDateString(),
            })));
            if (inventory.can_read_keys) {
                row.appendChild(document.createTextNode(' '));
                const button = el('button', 'btn btn--ghost', t('bitlocker.reveal'));
                button.type = 'button';
                button.addEventListener('click', () => reveal(volume, protector));
                row.appendChild(button);
            }
            if (protector.read_count) {
                row.appendChild(el('div', 'stat-card__meta', t('bitlocker.escrow.read_before', {
                    count: protector.read_count,
                    when: new Date(protector.last_read_at * 1000).toLocaleString(),
                })));
            }
            cell.appendChild(row);
        });
        return cell;
    }

    function draw() {
        bodyEl.replaceChildren();
        const volumes = inventory.volumes || [];

        if (inventory.support === 'unsupported') {
            noteEl.hidden = false;
            noteEl.textContent = t('bitlocker.unsupported');
            return;
        }
        if (inventory.support === 'error') {
            noteEl.hidden = false;
            noteEl.textContent = t('bitlocker.read_failed', {
                error: inventory.error || '',
            });
            return;
        }

        const table = el('table', 'data-table');
        const head = el('thead');
        const headRow = el('tr');
        [t('bitlocker.column.volume'), t('bitlocker.column.protection'),
         t('bitlocker.column.method'), t('bitlocker.column.key')]
            .forEach((label) => headRow.appendChild(el('th', null, label)));
        head.appendChild(headRow);
        table.appendChild(head);

        const body = el('tbody');
        volumes.forEach((volume) => {
            const tr = el('tr');
            tr.appendChild(el('td', null, volumeName(volume)));

            const state = el('td');
            state.appendChild(el('span', null, protectionLabel(volume.protection)));
            const conversion = conversionLabel(volume.conversion);
            // The second line is where a suspended volume stops looking encrypted: protection
            // reads `off` while conversion still reads `fully_encrypted`, which is exactly the
            // state Windows leaves a machine in after a firmware update.
            if (conversion) state.appendChild(el('div', 'stat-card__meta', conversion));
            tr.appendChild(state);

            tr.appendChild(el('td', null, volume.method || ''));
            tr.appendChild(escrowCell(volume));
            body.appendChild(tr);
        });
        table.appendChild(body);
        bodyEl.appendChild(table);

        // Keys the hub holds for protectors no volume claims any more. Shown rather than
        // hidden: "we still have a key for a disk that is gone" is a fact somebody should be
        // able to see, and a key nobody can see is a key nobody knows to clean up.
        if ((inventory.escrowed || []).length) {
            bodyEl.appendChild(el('p', 'stat-card__meta', t('bitlocker.orphaned', {
                count: inventory.escrowed.length,
            })));
        }
    }

    async function reveal(volume, protector) {
        errorEl.hidden = true;
        let response;
        try {
            response = await fetch(
                `/api/bitlocker/${encodeURIComponent(machine)}/reveal`,
                {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ protector_id: protector.id }),
                });
        } catch (e) {
            errorEl.hidden = false;
            errorEl.textContent = t('bitlocker.unreachable');
            return;
        }
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            errorEl.hidden = false;
            errorEl.textContent = data.error || t('bitlocker.reveal_failed');
            return;
        }
        keyEl.hidden = false;
        keyLabelEl.textContent = t('bitlocker.key_for', { volume: volumeName(volume) });
        keyValueEl.textContent = data.recovery_password;
        // Reload so the "read before" line under the button reflects what just happened --
        // this operator has now taken custody of that key, and the card should say so to the
        // next person who opens it without them having to find the audit log.
        load();
    }

    function render(data) {
        // `support: null` is "no agent has told us", which is every Windows PC until the agent
        // half ships and every machine that has not heartbeated since. The card is absent
        // rather than empty; an empty encryption card reads as a finding.
        fold.hidden = data.support === null;
        if (fold.hidden) return;

        inventory = data;
        noteEl.hidden = true;
        const encrypted = (data.volumes || [])
            .filter((v) => v.protection === 'on').length;
        summaryEl.textContent = t('bitlocker.summary', {
            encrypted: encrypted, total: (data.volumes || []).length,
        });
        draw();
    }

    async function load() {
        let response;
        try {
            response = await fetch(`/api/bitlocker/${encodeURIComponent(machine)}`);
        } catch (e) {
            return;
        }
        if (!response.ok) {
            fold.hidden = true;      // out of scope, which for this operator is "no such card"
            return;
        }
        render(await response.json());
    }

    load();
}());
