// Device pairing consent page (roadmap #11).
//
// One button. It posts the ticked capabilities to /app/pair/confirm, and then does ONE of
// two things with the answer:
//
//   * the hub returned a redirect  -> navigate to it, handing the one-time code to the
//     app's loopback listener (RFC 8252's native-app flow);
//   * it did not                   -> show the code for the operator to type into the app.
//
// Both paths end at the same single-use exchange, so the copy-paste fallback is not a
// second mechanism with rules of its own -- it is the same grant, collected by hand.
//
// Built with textContent/createElement like every other page here: the code is a secret
// and the device name is operator-supplied, and neither belongs in an innerHTML string.

const confirmBtn = document.getElementById('pair-confirm');
const statusEl = document.getElementById('pair-status');
const deviceLine = document.getElementById('pair-device-line');
const codeCard = document.getElementById('pair-code-card');
const codeEl = document.getElementById('pair-code');

function checkedCapabilities() {
    return Array.from(
        document.querySelectorAll('input[name="capability"]:checked')
    ).map((box) => box.value);
}

confirmBtn.addEventListener('click', async () => {
    const capabilities = checkedCapabilities();
    if (!capabilities.length) {
        statusEl.textContent = t('pair.pick_one');
        return;
    }

    confirmBtn.disabled = true;
    statusEl.textContent = t('common.saving');

    try {
        const resp = await fetch('/app/pair/confirm', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                capabilities,
                device_name: deviceLine.dataset.name || '',
                platform: deviceLine.dataset.platform || '',
                redirect: confirmBtn.dataset.redirect || '',
                state: confirmBtn.dataset.state || '',
            }),
        });
        let body = null;
        try { body = await resp.json(); } catch (e) { /* empty body is fine */ }
        if (!resp.ok) throw new Error((body && body.error) || `HTTP ${resp.status}`);

        if (body.redirect) {
            // The app is listening on loopback. Hand it the code and stop -- the page it
            // lands on is the app's own, not ours.
            statusEl.textContent = t('pair.handing_over');
            window.location.href = body.redirect;
            return;
        }

        statusEl.textContent = '';
        codeEl.textContent = body.code;
        codeCard.hidden = false;
        // The code is single-use, so re-pressing the button would mint a SECOND grant and
        // silently strand the one now on screen.
        confirmBtn.hidden = true;
    } catch (err) {
        statusEl.textContent = err.message;
        confirmBtn.disabled = false;
    }
});
