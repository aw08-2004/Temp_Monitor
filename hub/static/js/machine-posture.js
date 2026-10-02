// The machine page's Security posture card (roadmap #25 D): antivirus, firewall, AutoRun,
// screen lock, default and administrator accounts, encryption, Secure Boot and the TPM, each
// judged by the hub against the security.posture_* thresholds and labelled with the CIS
// Controls IG1 safeguard it is evidence for.
//
// **Three results, drawn as three.** Passed, failed and unknown each have their own pill, and
// unknown is grey rather than amber: it means "the machine did not say", which is a thing to
// look into, not a finding. A card that coloured unknowns as failures would be red on every PC
// whose WMI hiccupped once, and a helpdesk learns to ignore a red card in about a week.
//
// **No agent version gate**, for the reason machine-bitlocker.js gives: nothing here asks the
// machine to do anything. `posture: null` already means "no agent has told us", and the card
// is absent in that state rather than empty -- an empty compliance card reads as a finding.
//
// Nothing polls. A posture is re-read hourly on the machine.
(function () {
    'use strict';

    const fold = document.getElementById('card-posture');
    if (!fold || !window.MachineContext || !window.PostureLabels) return;

    const machine = window.MachineContext.current();
    if (!machine) return;

    const bodyEl = document.getElementById('posture-body');
    const summaryEl = document.getElementById('posture-summary');
    const reportedEl = document.getElementById('posture-reported');
    const errorEl = document.getElementById('posture-error');
    const labels = window.PostureLabels;

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function pill(status) {
        const variant = status === 'pass' ? 'status-pill--ok'
            : status === 'fail' ? 'status-pill--danger' : 'status-pill--muted';
        const node = el('span', `status-pill ${variant}`);
        node.appendChild(el('span', 'status-pill__dot'));
        node.appendChild(document.createTextNode(labels.statusLabel(status)));
        return node;
    }

    function draw(data) {
        const table = el('table', 'data-table');
        const headRow = el('tr');
        [t('posture.col.check'), t('posture.col.result'), t('posture.col.detail'),
         t('posture.col.cis')].forEach((label) => headRow.appendChild(el('th', null, label)));
        table.appendChild(el('thead')).appendChild(headRow);

        const body = el('tbody');
        data.checks.forEach((check) => {
            const tr = el('tr');
            tr.appendChild(el('td', null, labels.checkTitle(check.id)));
            const result = el('td');
            result.appendChild(pill(check.status));
            tr.appendChild(result);
            // Text from the machine (product and account names) arrives inside the detail's
            // parameters and is set with textContent like everything else here.
            tr.appendChild(el('td', null, labels.detailText(check)));
            tr.appendChild(el('td', null, check.cis || ''));
            body.appendChild(tr);
        });
        table.appendChild(body);
        bodyEl.replaceChildren(table);
    }

    function showError(text) {
        fold.hidden = false;
        errorEl.hidden = false;
        errorEl.replaceChildren(el('span', null, text), document.createTextNode(' '));
        const retry = el('button', 'btn btn--ghost', t('posture.retry'));
        retry.type = 'button';
        retry.addEventListener('click', () => load());
        errorEl.appendChild(retry);
    }

    async function load() {
        let response;
        try {
            response = await fetch(`/api/posture/machines/${encodeURIComponent(machine)}`);
        } catch (e) {
            showError(t('posture.unreachable'));
            return;
        }
        if (!response.ok) {
            // A 5xx is ours and worth a retry; a 4xx is a scope or capability refusal, and
            // the card simply is not this operator's to see.
            if (response.status >= 500) showError(t('posture.load_error'));
            else fold.hidden = true;
            return;
        }
        const data = await response.json();
        errorEl.hidden = true;
        fold.hidden = !data.checks;
        if (fold.hidden) return;
        summaryEl.textContent = t('posture.summary', data.counts || {});
        reportedEl.textContent = data.reported_at
            ? t('posture.reported', { when: new Date(data.reported_at * 1000).toLocaleString() })
            : '';
        draw(data);
    }

    load();
}());
