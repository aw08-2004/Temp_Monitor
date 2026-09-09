// The Android device-owner provisioning QR (roadmap #23 phase A).
//
// Three rules govern this file, and all three are about the same fact: the code drawn here is
// scanned at the setup wizard of a device that has ALREADY been factory reset, and a payload
// that is subtly wrong is discovered several minutes later, on a wiped phone, with no way
// forward but another wipe.
//
//   1. **The string in the QR comes from the hub, verbatim.** The API returns both a `payload`
//      object (for the table) and a `text` string (for the code), and only `text` is ever
//      encoded. Rebuilding it here with JSON.stringify would order the keys by insertion and
//      produce a code that differs from the one the hub audited -- same bytes, different
//      string, and no way to tell them apart afterwards.
//   2. **Nothing is drawn from a half-configured payload.** A 409 renders the reason and no
//      code at all. A greyed-out or placeholder QR still scans.
//   3. **No innerHTML anywhere on this page.** The payload used to carry an operator-supplied
//      URL; it now carries this hub's own, and what is operator-supplied instead is the
//      uploaded file's NAME, which the card below renders. The encoder's own
//      createSvgTag/createImgTag helpers are not used for the same reason -- isDark() plus a
//      canvas is fifteen lines and takes no markup at all.
(function () {
    'use strict';

    const canvas = document.getElementById('qr-canvas');
    if (!canvas) return;

    const loading = document.getElementById('qr-loading');
    const unconfigured = document.getElementById('qr-unconfigured');
    const reason = document.getElementById('qr-reason');
    const ready = document.getElementById('qr-ready');
    const payloadHost = document.getElementById('payload-host');
    const copyButton = document.getElementById('copy-payload');
    const printButton = document.getElementById('print-qr');
    const digestInput = document.getElementById('digest-input');
    const convertButton = document.getElementById('convert-digest');
    const digestResult = document.getElementById('digest-result');

    // The string the QR encodes. Held so Copy hands over exactly what was scanned rather than
    // a re-serialisation of the table beside it.
    let qrText = '';

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    // ---------------------------------------------------------------- the code itself
    //
    // Type 0 is the encoder's auto-select: it picks the smallest version that holds the data
    // at the given error-correction level. Fixing a version instead would work until somebody
    // set a longer APK URL, at which point addData throws and the page shows nothing.
    //
    // Error correction 'M' (~15%) rather than 'L'. A provisioning QR is scanned once, often
    // from a screen at an angle or from a sheet of paper taped to a bench, by whatever camera
    // the device happens to have -- and a failed scan at that point means starting the setup
    // wizard again. The extra correction costs a slightly denser code and buys tolerance for
    // exactly the conditions this is always used in.
    const ERROR_CORRECTION = 'M';

    function drawQr(text) {
        const code = qrcode(0, ERROR_CORRECTION);
        code.addData(text);
        code.make();

        const count = code.getModuleCount();
        // A 4-module quiet zone is required by the specification, not decoration: without it
        // many scanners cannot find the code's edges at all.
        const quiet = 4;
        // Sized so the whole thing lands near 480px on a normal display, then rounded DOWN to
        // a whole number of device pixels per module. A fractional module size is what makes a
        // rendered QR look soft and scan badly: the browser antialiases the boundary between
        // two modules into grey, and grey is neither dark nor light to a decoder.
        //
        // 480 rather than something smaller because of what a real payload measures. It was
        // ~565 bytes when the APK URL was typed in by an operator; now that the hub hosts the
        // file the URL carries a 43-character download token, and a real payload is ~630 --
        // around a version-21 code, 101 modules across. At 420px that is three device pixels
        // per module, which a phone camera reads from a screen only in good light and straight
        // on; 480 keeps it at four. That is the difference between scanning first time and
        // scanning on the fourth attempt, at a device that has already been wiped.
        const scale = Math.max(3, Math.floor(480 / (count + quiet * 2)));
        const size = (count + quiet * 2) * scale;

        canvas.width = size;
        canvas.height = size;
        const ctx = canvas.getContext('2d');
        // Painted explicitly rather than left transparent. A transparent canvas shows the
        // page's own background through it, which in the dark theme is a QR no scanner reads.
        ctx.fillStyle = '#ffffff';
        ctx.fillRect(0, 0, size, size);
        ctx.fillStyle = '#000000';
        for (let row = 0; row < count; row += 1) {
            for (let col = 0; col < count; col += 1) {
                if (code.isDark(row, col)) {
                    ctx.fillRect((col + quiet) * scale, (row + quiet) * scale, scale, scale);
                }
            }
        }
    }

    // ---------------------------------------------------------------- the payload table
    function renderPayload(payload, component) {
        payloadHost.replaceChildren();
        const table = el('table', 'data-table');
        const body = el('tbody');

        // The component is shown first and on its own, because it is the field that cannot be
        // corrected after the fact: it is baked into a printed code, and a device that reads a
        // component name no build carries cannot finish provisioning.
        const head = el('thead');
        const headRow = el('tr');
        headRow.append(el('th', null, t('provisioning.payload.field')),
                       el('th', null, t('provisioning.payload.value')));
        head.append(headRow);
        table.append(head);

        const rows = [[t('provisioning.payload.component'), component]];
        Object.keys(payload || {}).sort().forEach((key) => {
            const value = payload[key];
            rows.push([
                // The raw extra name, deliberately, not a friendly label: this is what an
                // operator compares against a vendor's provisioning documentation when a
                // device rejects the code, and a translated caption would not match anything.
                key.replace('android.app.extra.PROVISIONING_', ''),
                (value !== null && typeof value === 'object')
                    ? Object.keys(value).sort().map((k) => `${k}=${value[k]}`).join(', ')
                    : String(value),
            ]);
        });

        rows.forEach(([field, value]) => {
            const row = el('tr');
            row.append(el('td', null, field));
            const cell = el('td', null, value);
            cell.style.fontFamily = 'var(--font-mono)';
            cell.style.wordBreak = 'break-all';
            row.append(cell);
            body.append(row);
        });
        table.append(body);
        payloadHost.append(table);
    }

    // ---------------------------------------------------------------- load
    async function load() {
        let response;
        try {
            response = await fetch('/api/provisioning/qr');
        } catch (e) {
            loading.textContent = t('provisioning.qr.unreachable');
            return;
        }
        const data = await response.json().catch(() => ({}));
        loading.hidden = true;

        if (!response.ok) {
            // 409 is the ordinary state of a hub nobody has configured yet, and the message is
            // instructions rather than an error. Anything else still lands here with its own
            // reason, which is better than a generic failure.
            reason.textContent = data.error || t('provisioning.qr.failed');
            unconfigured.hidden = false;
            return;
        }

        qrText = data.text || '';
        try {
            drawQr(qrText);
        } catch (e) {
            // The one failure the encoder can still produce: a payload too large for any QR
            // version. Reported rather than swallowed, because the page would otherwise show
            // an empty white square that looks like a rendering glitch.
            reason.textContent = t('provisioning.qr.too_large');
            unconfigured.hidden = false;
            return;
        }
        renderPayload(data.payload, data.component);
        ready.hidden = false;
    }

    // ---------------------------------------------------------------- actions
    copyButton.addEventListener('click', async () => {
        try {
            await navigator.clipboard.writeText(qrText);
            copyButton.textContent = t('provisioning.qr.copied');
            setTimeout(() => { copyButton.textContent = t('provisioning.qr.copy'); }, 1500);
        } catch (e) {
            // Clipboard access is refused on an insecure origin and in some locked-down
            // browsers. Saying so beats a button that silently does nothing.
            copyButton.textContent = t('provisioning.qr.copy_failed');
        }
    });

    printButton.addEventListener('click', () => window.print());

    convertButton.addEventListener('click', async () => {
        digestResult.textContent = '';
        const digest = (digestInput.value || '').trim();
        if (!digest) return;
        let response;
        try {
            response = await fetch('/api/provisioning/checksum', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ digest }),
            });
        } catch (e) {
            digestResult.textContent = t('provisioning.qr.unreachable');
            return;
        }
        const data = await response.json().catch(() => ({}));
        // The refusal text is rendered verbatim: provisioning.py names the specific mistake
        // (a hex digest pasted where base64url belongs, a digest of the wrong length), and a
        // generic "invalid" here would throw away the only part that helps.
        digestResult.textContent = response.ok
            ? data.checksum
            : (data.error || t('provisioning.checksum.failed'));
    });

    // ---------------------------------------------------------------- the hosted APK
    //
    // The upload that replaces two hand-typed fields. Rule 3 at the top of this file applies
    // with full force here: the file name is whatever an operator called the file, so it
    // reaches the page through textContent and nothing else.
    const apkLoading = document.getElementById('apk-loading');
    const apkEmpty = document.getElementById('apk-empty');
    const apkHostedPane = document.getElementById('apk-hosted');
    const apkDetail = document.getElementById('apk-detail');
    const apkFile = document.getElementById('apk-file');
    const apkUpload = document.getElementById('apk-upload');
    const apkRemove = document.getElementById('apk-remove');
    const apkStatus = document.getElementById('apk-status');

    function megabytes(bytes) {
        return `${(Number(bytes || 0) / (1024 * 1024)).toFixed(1)} MB`;
    }

    function renderApk(data) {
        apkLoading.hidden = true;
        apkDetail.replaceChildren();
        apkEmpty.hidden = Boolean(data.hosted);
        apkHostedPane.hidden = !data.hosted;
        apkRemove.hidden = !data.hosted;
        if (!data.hosted) return;

        const table = el('table', 'data-table');
        const body = el('tbody');
        [
            [t('provisioning.apk.file_name'), data.file_name],
            [t('provisioning.apk.size'), megabytes(data.size_bytes)],
            [t('provisioning.apk.sha256'), data.sha256],
            // The row this whole feature exists to fill in. Shown so an operator can compare it
            // against what apksigner prints, using the converter below.
            [t('provisioning.apk.checksum'), data.checksum],
            [t('provisioning.apk.download_url'), data.download_url],
            [t('provisioning.apk.uploaded'),
             `${new Date((data.uploaded_at || 0) * 1000).toLocaleString()} ${data.uploaded_by || ''}`],
        ].forEach(([field, value]) => {
            const row = el('tr');
            row.append(el('td', null, field));
            const cell = el('td', null, value);
            cell.style.fontFamily = 'var(--font-mono)';
            cell.style.wordBreak = 'break-all';
            row.append(cell);
            body.append(row);
        });
        table.append(body);
        apkDetail.append(table);
    }

    async function loadApk() {
        let response;
        try {
            response = await fetch('/api/provisioning/apk');
        } catch (e) {
            apkLoading.textContent = t('provisioning.qr.unreachable');
            return;
        }
        if (!response.ok) {
            apkLoading.textContent = t('provisioning.apk.failed');
            return;
        }
        renderApk(await response.json());
    }

    /** Redraw the code as well as the card. An upload that left a stale QR on screen would be
     *  showing a code for an APK this hub no longer serves -- which scans, and then fails. */
    async function reloadBoth() {
        loading.hidden = false;
        ready.hidden = true;
        unconfigured.hidden = true;
        await Promise.all([loadApk(), load()]);
    }

    apkUpload.addEventListener('click', async () => {
        apkStatus.textContent = '';
        const file = apkFile.files && apkFile.files[0];
        if (!file) {
            apkStatus.textContent = t('provisioning.apk.choose_first');
            return;
        }

        const form = new FormData();
        form.append('file', file);
        // No Content-Type header: the browser sets the multipart boundary, and naming the type
        // by hand produces a body the server cannot parse.
        apkUpload.disabled = true;
        // Ten megabytes with no feedback reads as a dead page, and the instinct that follows is
        // to click again.
        apkStatus.textContent = t('provisioning.apk.uploading');
        let response;
        try {
            response = await fetch('/api/provisioning/apk', { method: 'POST', body: form });
        } catch (e) {
            apkUpload.disabled = false;
            apkStatus.textContent = t('provisioning.qr.unreachable');
            return;
        }
        apkUpload.disabled = false;
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            // Verbatim: the parser names the exact cause -- signed only with a v1 signature,
            // two signers, a v2 and v3 block that disagree -- and that sentence is the only
            // part that tells somebody what to do next.
            apkStatus.textContent = data.error || t('provisioning.apk.failed');
            return;
        }
        apkFile.value = '';
        apkStatus.textContent = t('provisioning.apk.uploaded_ok');
        await reloadBoth();
    });

    apkRemove.addEventListener('click', async () => {
        if (!window.confirm(t('provisioning.apk.remove_confirm'))) return;
        apkStatus.textContent = '';
        try {
            await fetch('/api/provisioning/apk', { method: 'DELETE' });
        } catch (e) {
            apkStatus.textContent = t('provisioning.qr.unreachable');
            return;
        }
        await reloadBoth();
    });

    loadApk();
    load();
})();
