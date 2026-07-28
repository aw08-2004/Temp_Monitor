// Remote view/control (roadmap #2). The console side of the WebRTC session: it starts a
// session, answers the agent helper's offer, renders the incoming video, and sends input and
// live quality changes back over the agent-created "control" DataChannel.
//
// The agent is the offerer (it has the media), so the browser is the ANSWERER: it polls the
// hub for the agent's offer + trickled ICE, answers, and trickles its own ICE back. Signaling
// is plain HTTP polling through /api/remote/* -- same model as the fleet terminal, and the hub
// relays between the two sides (see remote_web.py).
//
// Two classes of setting, and the split is not cosmetic:
//   * START-TIME (Windows session, codec, encoder) is negotiated in the SDP or decides which
//     encoder object the agent builds, so changing it requires a new session. The UI disables
//     those controls while connected rather than letting a change silently do nothing.
//   * LIVE (monitor, fps, bitrate, scale) rides the control channel as a {t:'cfg'} message and
//     the agent rebuilds its capture pipeline in place.
(function () {
    'use strict';

    const card = document.getElementById('remote-card');
    if (!card || !window.FleetApi) return;
    const MACHINE = window.FleetApi.machine;

    const els = {
        start: document.getElementById('remote-start'),
        stop: document.getElementById('remote-stop'),
        cad: document.getElementById('remote-cad'),
        status: document.getElementById('remote-status'),
        video: document.getElementById('remote-video'),
        stage: document.getElementById('remote-stage'),
        overlay: document.getElementById('remote-overlay'),
        overlayStatus: document.getElementById('remote-overlay-status'),
        exitFullscreen: document.getElementById('remote-exit-fullscreen'),
        hint: document.getElementById('remote-hint'),
        meta: document.getElementById('remote-meta'),
        desktopBadge: document.getElementById('remote-desktop-badge'),
        headlessBadge: document.getElementById('remote-headless-badge'),
        session: document.getElementById('remote-session'),
        refreshSessions: document.getElementById('remote-refresh-sessions'),
        codec: document.getElementById('remote-codec'),
        encoder: document.getElementById('remote-encoder'),
        monitor: document.getElementById('remote-monitor'),
        preset: document.getElementById('remote-preset'),
        fps: document.getElementById('remote-fps'),
        bitrate: document.getElementById('remote-bitrate'),
        scale: document.getElementById('remote-scale'),
        viewOnly: document.getElementById('remote-viewonly'),
        fullscreen: document.getElementById('remote-fullscreen'),
        vdd: document.getElementById('remote-vdd'),
        vddText: document.getElementById('remote-vdd-text'),
        vddInstall: document.getElementById('remote-vdd-install'),
        vddUninstall: document.getElementById('remote-vdd-uninstall'),
    };

    const POLL_INTERVAL_MS = 800;
    const MOVE_THROTTLE_MS = 40;   // ~25 mouse-move messages/sec is plenty and won't flood

    // Presets exist because "15fps / 4000kbps / 100%" means nothing to someone who just wants
    // the screen to stop stuttering. Custom reveals the raw numbers for when it does matter.
    const PRESETS = {
        quality:  { fps: 25, bitrate_kbps: 8000, scale: 100 },
        balanced: { fps: 15, bitrate_kbps: 4000, scale: 100 },
        speed:    { fps: 10, bitrate_kbps: 1500, scale: 50 },
    };

    let pc = null;
    let controlChannel = null;
    let sessionId = null;
    let afterSeq = 0;
    let pollTimer = null;
    let remoteSet = false;
    let pendingIce = [];
    let running = false;
    // Capture geometry as last reported by the agent over the control channel. Needed to map
    // pointer coordinates correctly once the video is letterboxed (object-fit: contain).
    let captured = { w: 0, h: 0 };

    function setStatus(text, kind) {
        els.status.className = 'status-pill status-pill--' + (kind || 'muted');
        els.status.innerHTML = '<span class="status-pill__dot"></span>';
        els.status.append(text);
        els.overlayStatus.textContent = text;
    }

    function hint(text) { els.hint.textContent = text || ''; }
    function meta(text) { els.meta.textContent = text || ''; }

    // ---- Start-time settings -------------------------------------------------------------
    function startTimeControls() {
        return [els.session, els.codec, els.encoder, els.refreshSessions];
    }

    function lockStartTimeControls(locked) {
        startTimeControls().forEach((el) => { el.disabled = locked; });
    }

    function liveSettings() {
        const preset = els.preset.value;
        const base = PRESETS[preset] || {
            fps: clampInt(els.fps.value, 1, 60, 15),
            bitrate_kbps: clampInt(els.bitrate.value, 100, 50000, 4000),
            scale: clampInt(els.scale.value, 25, 100, 100),
        };
        return Object.assign({ monitor: clampInt(els.monitor.value, 0, 15, 0) }, base);
    }

    function clampInt(value, low, high, fallback) {
        const n = parseInt(value, 10);
        if (!Number.isFinite(n)) return fallback;
        return Math.min(high, Math.max(low, n));
    }

    // Keep the custom fields in step with the chosen preset, so switching to Custom starts
    // from what you were just watching rather than from stale defaults.
    function syncPresetFields() {
        const custom = els.preset.value === 'custom';
        card.querySelectorAll('.remote-field--custom').forEach((el) => { el.hidden = !custom; });
        if (!custom) {
            const preset = PRESETS[els.preset.value];
            if (preset) {
                els.fps.value = preset.fps;
                els.bitrate.value = preset.bitrate_kbps;
                els.scale.value = String(preset.scale);
            }
        }
    }

    // ---- Session lifecycle ---------------------------------------------------------------
    async function start() {
        if (running) return;
        running = true;
        els.start.disabled = true;
        els.stop.disabled = false;
        lockStartTimeControls(true);
        setStatus('Starting…', 'warn');
        hint('Waiting for the agent to bring up its capture helper…');
        afterSeq = 0;
        remoteSet = false;
        pendingIce = [];
        captured = { w: 0, h: 0 };
        try {
            const body = Object.assign({
                session: els.session.value || 'auto',
                codec: els.codec.value,
                encoder: els.encoder.value,
            }, liveSettings());
            const res = await window.FleetApi.postJson(
                `/api/remote/${encodeURIComponent(MACHINE)}/start`, body);
            sessionId = res.session_id;
            createPeer(res.ice_servers || []);
            schedulePoll();
        } catch (e) {
            hint('Could not start: ' + e.message);
            teardown('failed');
        }
    }

    function createPeer(iceServers) {
        pc = new RTCPeerConnection({ iceServers });

        pc.ontrack = (e) => {
            if (e.streams && e.streams[0]) els.video.srcObject = e.streams[0];
        };
        // The agent (offerer) creates the "control" channel. It carries input UP and status
        // (geometry, desktop switches, capture stalls) DOWN.
        pc.ondatachannel = (e) => {
            if (e.channel.label !== 'control') return;
            controlChannel = e.channel;
            controlChannel.onopen = () => {
                els.cad.disabled = false;
                // Re-assert the live settings: the agent started with what the hub queued, but
                // the operator may have changed a control while we were connecting.
                sendConfig();
            };
            controlChannel.onclose = () => { els.cad.disabled = true; };
            controlChannel.onmessage = (msg) => handleAgentStatus(msg.data);
        };
        pc.onicecandidate = (e) => {
            if (!e.candidate || !sessionId) return;
            const c = e.candidate;
            postSignal('ice', {
                candidate: c.candidate,
                sdpMid: c.sdpMid,
                sdpMLineIndex: c.sdpMLineIndex,
            });
        };
        pc.onconnectionstatechange = () => {
            switch (pc.connectionState) {
                case 'connecting': setStatus('Connecting…', 'warn'); break;
                case 'connected': setStatus('Live', 'ok'); hint(''); break;
                case 'disconnected': setStatus('Reconnecting…', 'warn'); break;
                case 'failed': hint('Connection failed.'); teardown('failed'); break;
                case 'closed': break;
            }
        };
    }

    // Status the agent pushes down the control channel. Without this, "the screen went black"
    // is unguessable from here -- and the agent already knows why.
    function handleAgentStatus(raw) {
        let msg;
        try { msg = JSON.parse(raw); } catch (e) { return; }
        if (!msg || typeof msg !== 'object') return;

        if (msg.t === 'geom') {
            captured = { w: msg.w || 0, h: msg.h || 0 };
            const secure = msg.desktop && msg.desktop.toLowerCase() !== 'default';
            els.desktopBadge.hidden = !secure;
            if (secure) {
                els.desktopBadge.lastChild.textContent =
                    msg.desktop === 'Winlogon' ? 'Lock / logon screen' : msg.desktop;
            }
            populateMonitors(msg.monitors, msg.monitor);
            meta(`${msg.w}×${msg.h} · ${msg.encoder || ''} · desktop ${msg.desktop || '?'}`);
            hint('');
        } else if (msg.t === 'capture' && msg.state === 'stalled') {
            hint('The agent is not getting any frames' +
                 (msg.desktop ? ` on the ${msg.desktop} desktop` : '') +
                 '. If this machine has no monitor, install a virtual display below.');
        }
    }

    function populateMonitors(count, current) {
        const n = Math.max(1, count || 1);
        if (els.monitor.options.length === n) return;
        const selected = current != null ? String(current) : els.monitor.value;
        els.monitor.innerHTML = '';
        for (let i = 0; i < n; i++) {
            const option = document.createElement('option');
            option.value = String(i);
            option.textContent = String(i + 1);
            els.monitor.appendChild(option);
        }
        els.monitor.value = selected;
    }

    function postSignal(kind, payload) {
        if (!sessionId) return Promise.resolve();
        return window.FleetApi.postJson(
            `/api/remote/session/${encodeURIComponent(sessionId)}/signal`,
            { kind, payload }
        ).catch(() => { /* transient; the next tick retries the relevant state */ });
    }

    function schedulePoll() {
        if (!running) return;
        pollTimer = setTimeout(poll, POLL_INTERVAL_MS);
    }

    async function poll() {
        if (!running || !sessionId) return;
        try {
            const res = await window.FleetApi.getJson(
                `/api/remote/session/${encodeURIComponent(sessionId)}/poll?after_seq=${afterSeq}`);
            afterSeq = res.next_seq;
            for (const sig of res.signals || []) await handleSignal(sig);
            if (res.status === 'ended' || res.status === 'expired') {
                hint('Session ' + res.status + '.');
                teardown(res.status === 'expired' ? 'warn' : 'muted');
                return;
            }
        } catch (e) {
            // Keep polling through transient errors; a real end comes via status above.
        }
        schedulePoll();
    }

    async function handleSignal(sig) {
        if (!pc) return;
        try {
            if (sig.kind === 'offer') {
                await pc.setRemoteDescription({ type: 'offer', sdp: sig.payload.sdp });
                remoteSet = true;
                for (const ice of pendingIce) await pc.addIceCandidate(ice).catch(() => {});
                pendingIce = [];
                const answer = await pc.createAnswer();
                await pc.setLocalDescription(answer);
                await postSignal('answer', { type: 'answer', sdp: answer.sdp });
            } else if (sig.kind === 'ice') {
                const cand = {
                    candidate: sig.payload.candidate,
                    sdpMid: sig.payload.sdpMid,
                    sdpMLineIndex: sig.payload.sdpMLineIndex,
                };
                if (remoteSet) await pc.addIceCandidate(cand).catch(() => {});
                else pendingIce.push(cand);
            } else if (sig.kind === 'bye') {
                teardown('muted');
            }
        } catch (e) {
            hint('Signaling error: ' + e.message);
        }
    }

    async function stop() {
        if (sessionId) {
            try {
                await window.FleetApi.postJson(
                    `/api/remote/session/${encodeURIComponent(sessionId)}/stop`, {});
            } catch (e) { /* best effort */ }
        }
        teardown('muted');
    }

    function teardown(statusKind) {
        running = false;
        if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
        if (pc) { try { pc.close(); } catch (e) {} pc = null; }
        controlChannel = null;
        if (els.video.srcObject) {
            els.video.srcObject.getTracks().forEach((t) => t.stop());
            els.video.srcObject = null;
        }
        sessionId = null;
        remoteSet = false;
        pendingIce = [];
        captured = { w: 0, h: 0 };
        els.start.disabled = false;
        els.stop.disabled = true;
        els.cad.disabled = true;
        els.desktopBadge.hidden = true;
        lockStartTimeControls(false);
        meta('');
        setStatus(statusKind === 'failed' ? 'Failed' : 'Idle',
                  statusKind === 'failed' ? 'danger' : (statusKind || 'muted'));
    }

    // ---- Live configuration --------------------------------------------------------------
    function sendConfig() {
        const settings = liveSettings();
        sendControl(Object.assign({ t: 'cfg' }, settings));
    }

    function sendControl(obj) {
        if (controlChannel && controlChannel.readyState === 'open') {
            try { controlChannel.send(JSON.stringify(obj)); } catch (e) { /* dropped */ }
        }
    }

    // ---- Input capture -------------------------------------------------------------------
    function sendInput(obj) {
        // View-only is a VIEWER-SIDE guard against accidental clicks and stray keystrokes.
        // It is deliberately NOT a security control: the agent still accepts input on this
        // channel, so anyone who can open a session can drive the machine. If you need that
        // to be untrue, it has to be enforced at the hub and the agent, not here.
        if (els.viewOnly.checked) return;
        sendControl(obj);
    }

    // Normalised (0..1) position within the CAPTURED IMAGE, not the video element.
    //
    // The element is object-fit: contain, so the picture is letterboxed whenever its aspect
    // ratio differs from the box -- always in fullscreen, and whenever the remote machine
    // changes resolution mid-stream (the lock screen, a virtual display coming up). Mapping
    // element coordinates straight through would put every click off by the size of the bars.
    // Returns null for a click in the letterbox, which is not on the remote desktop at all.
    function normPos(e) {
        const rect = els.video.getBoundingClientRect();
        const vw = els.video.videoWidth || captured.w;
        const vh = els.video.videoHeight || captured.h;
        if (!vw || !vh || !rect.width || !rect.height) return null;

        const scale = Math.min(rect.width / vw, rect.height / vh);
        const shownW = vw * scale;
        const shownH = vh * scale;
        const offsetX = (rect.width - shownW) / 2;
        const offsetY = (rect.height - shownH) / 2;

        const x = (e.clientX - rect.left - offsetX) / shownW;
        const y = (e.clientY - rect.top - offsetY) / shownH;
        if (x < 0 || x > 1 || y < 0 || y > 1) return null;
        return [x, y];
    }

    function wireInput() {
        const v = els.video;
        v.tabIndex = 0;   // make it focusable so it can receive key events
        let lastMove = 0;

        v.addEventListener('mousemove', (e) => {
            const now = performance.now();
            if (now - lastMove < MOVE_THROTTLE_MS) return;
            lastMove = now;
            const pos = normPos(e);
            if (pos) sendInput({ t: 'm', x: pos[0], y: pos[1] });
        });
        v.addEventListener('mousedown', (e) => {
            v.focus();
            const pos = normPos(e);
            if (pos) sendInput({ t: 'd', b: e.button, x: pos[0], y: pos[1] });
            e.preventDefault();
        });
        v.addEventListener('mouseup', (e) => {
            const pos = normPos(e);
            if (pos) sendInput({ t: 'u', b: e.button, x: pos[0], y: pos[1] });
            e.preventDefault();
        });
        v.addEventListener('contextmenu', (e) => e.preventDefault());
        v.addEventListener('wheel', (e) => {
            sendInput({ t: 'w', dy: -Math.sign(e.deltaY) });
            e.preventDefault();
        }, { passive: false });
        // Only intercept keys while the video is focused, so the operator can still use the rest
        // of the page normally.
        v.addEventListener('keydown', (e) => {
            sendInput({ t: 'k', code: e.code, key: e.key, down: true });
            e.preventDefault();
        });
        v.addEventListener('keyup', (e) => {
            sendInput({ t: 'k', code: e.code, key: e.key, down: false });
            e.preventDefault();
        });
    }

    // ---- Inventory: session picker + headless badge ---------------------------------------
    async function loadInventory() {
        let data;
        try {
            data = await window.FleetApi.getJson(
                `/api/remote/${encodeURIComponent(MACHINE)}/inventory`);
        } catch (e) {
            return;   // an agent too old to report leaves the picker on "Auto", which still works
        }
        renderSessions(data.sessions || []);
        renderDisplays(data.displays || {}, data.payload_available);
    }

    function renderSessions(sessions) {
        const selected = els.session.value;
        els.session.innerHTML = '';
        els.session.appendChild(new Option('Auto (agent picks)', 'auto'));
        for (const s of sessions) {
            // A session with nobody signed in is the logon screen -- and on a headless machine
            // that is exactly the one the operator needs, so it is labelled, not hidden.
            const who = s.is_logon_screen ? 'logon screen' : (s.account || 'no user');
            const bits = [s.state];
            if (s.is_console) bits.push('console');
            if (s.client) bits.push(s.client);
            els.session.appendChild(
                new Option(`Session ${s.id} — ${who} (${bits.join(', ')})`, String(s.id)));
        }
        // Keep the operator's choice across refreshes when it still exists.
        els.session.value =
            Array.from(els.session.options).some((o) => o.value === selected) ? selected : 'auto';
    }

    function renderDisplays(displays, payloadAvailable) {
        const headless = !!displays.headless;
        const present = !!displays.virtual_display_present;
        els.headlessBadge.hidden = !headless;

        // The panel is shown when there is a decision to make: nothing to capture (install), or
        // a virtual display already here (remove it once a real monitor shows up).
        els.vdd.hidden = !(headless || present);
        els.vddInstall.hidden = present;
        els.vddUninstall.hidden = !present;

        if (present) {
            els.vddText.textContent =
                `A virtual display is installed${displays.virtual_display_started ? '' : ' but NOT started'}. ` +
                `Physical monitors: ${displays.physical_monitors}.`;
        } else if (headless) {
            els.vddText.textContent = payloadAvailable
                ? 'This machine reports no monitors, so there is nothing for the screen capture ' +
                  'to duplicate and the stream will be black. Installing a virtual display gives ' +
                  'the desktop and the logon screen somewhere to be drawn.'
                : 'This machine reports no monitors, so the stream will be black. No virtual ' +
                  'display driver has been uploaded yet — upload it on the Packages page and pin ' +
                  'it in Settings › Remote.';
            els.vddInstall.disabled = !payloadAvailable;
        }
    }

    async function refreshInventory() {
        els.refreshSessions.disabled = true;
        hint('Asking the machine to re-report its sessions and displays…');
        try {
            await window.FleetApi.postJson(
                `/api/remote/${encodeURIComponent(MACHINE)}/inventory/refresh`, {});
            // The agent picks the command up on its next poll and answers on its next
            // heartbeat, so give it a beat before reading back rather than showing stale data.
            setTimeout(() => { loadInventory(); hint(''); }, 4000);
        } catch (e) {
            hint('Could not refresh: ' + e.message);
        } finally {
            setTimeout(() => { els.refreshSessions.disabled = running; }, 4000);
        }
    }

    async function virtualDisplay(mode) {
        const button = mode === 'install' ? els.vddInstall : els.vddUninstall;
        button.disabled = true;
        hint(mode === 'install' ? 'Queuing the virtual display install…'
                                : 'Queuing the virtual display removal…');
        try {
            await window.FleetApi.postJson(
                `/api/remote/${encodeURIComponent(MACHINE)}/virtual-display`,
                { mode, monitors: 1, resolutions: [{ width: 1920, height: 1080, hz: 60 }] });
            hint('Queued. Watch the command result on the Commands tab; the machine page ' +
                 'updates once the agent reports back.');
            setTimeout(loadInventory, 15000);
        } catch (e) {
            hint('Could not queue: ' + e.message);
        } finally {
            button.disabled = false;
        }
    }

    // ---- Fullscreen ----------------------------------------------------------------------
    function toggleFullscreen() {
        if (document.fullscreenElement) {
            document.exitFullscreen().catch(() => {});
        } else {
            els.stage.requestFullscreen().catch((e) => hint('Fullscreen refused: ' + e.message));
        }
    }

    document.addEventListener('fullscreenchange', () => {
        const full = document.fullscreenElement === els.stage;
        els.overlay.hidden = !full;
        els.fullscreen.textContent = full ? 'Exit fullscreen' : 'Fullscreen';
        // Focus the video so keystrokes go to the remote machine rather than the page.
        if (full) els.video.focus();
    });

    // ---- Wiring --------------------------------------------------------------------------
    wireInput();
    syncPresetFields();
    loadInventory();

    els.start.addEventListener('click', start);
    els.stop.addEventListener('click', stop);
    els.cad.addEventListener('click', () => sendControl({ t: 'cad' }));
    els.refreshSessions.addEventListener('click', refreshInventory);
    els.fullscreen.addEventListener('click', toggleFullscreen);
    els.exitFullscreen.addEventListener('click', toggleFullscreen);
    els.vddInstall.addEventListener('click', () => virtualDisplay('install'));
    els.vddUninstall.addEventListener('click', () => virtualDisplay('uninstall'));

    els.preset.addEventListener('change', () => { syncPresetFields(); sendConfig(); });
    [els.monitor, els.fps, els.bitrate, els.scale].forEach((el) => {
        el.addEventListener('change', sendConfig);
    });
    els.viewOnly.addEventListener('change', () => {
        hint(els.viewOnly.checked
            ? 'View only: input from this browser is suppressed. This is an accident guard, ' +
              'not a permission — it does not stop anyone else driving the machine.'
            : '');
    });

    // Ending the session when the operator navigates away is best-effort -- the hub's TTL
    // sweep is the backstop if this never fires (e.g. a crash).
    window.addEventListener('beforeunload', () => {
        if (sessionId && navigator.sendBeacon) {
            // Can't set a JSON content-type on sendBeacon, so the hub's stop endpoint would
            // reject it; rely on the TTL sweep instead. Close the peer locally at least.
            if (pc) { try { pc.close(); } catch (e) {} }
        }
    });
})();
