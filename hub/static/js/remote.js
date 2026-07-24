// Remote view/control viewer (roadmap #2). The console side of the WebRTC session: it starts
// a session, then answers the agent helper's offer and renders the incoming H.264 video.
//
// The agent is the offerer (it has the media), so the browser is the ANSWERER: it polls the
// hub for the agent's offer + trickled ICE, answers, and trickles its own ICE back. Signaling
// is plain HTTP polling through /api/remote/* -- same model as the fleet terminal, and the hub
// relays between the two sides (see remote_web.py). Input control (mouse/keyboard) lands in
// phase 5; this is view-only.
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
        hint: document.getElementById('remote-hint'),
    };

    const POLL_INTERVAL_MS = 800;
    const MOVE_THROTTLE_MS = 40;   // ~25 mouse-move messages/sec is plenty and won't flood

    let pc = null;
    let controlChannel = null;
    let sessionId = null;
    let afterSeq = 0;
    let pollTimer = null;
    let remoteSet = false;
    let pendingIce = [];
    let running = false;

    function setStatus(text, kind) {
        els.status.className = 'status-pill status-pill--' + (kind || 'muted');
        els.status.innerHTML = '<span class="status-pill__dot"></span>';
        els.status.append(text);
    }

    function hint(text) { els.hint.textContent = text || ''; }

    async function start() {
        if (running) return;
        running = true;
        els.start.disabled = true;
        els.stop.disabled = false;
        setStatus('Starting…', 'warn');
        hint('Waiting for the agent to bring up its capture helper…');
        afterSeq = 0;
        remoteSet = false;
        pendingIce = [];
        try {
            const res = await window.FleetApi.postJson(
                `/api/remote/${encodeURIComponent(MACHINE)}/start`, { monitor: 0 });
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
        // The agent (offerer) creates the "control" channel; we send input events on it.
        pc.ondatachannel = (e) => {
            if (e.channel.label !== 'control') return;
            controlChannel = e.channel;
            controlChannel.onopen = () => { els.cad.disabled = false; };
            controlChannel.onclose = () => { els.cad.disabled = true; };
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
        els.start.disabled = false;
        els.stop.disabled = true;
        els.cad.disabled = true;
        setStatus(statusKind === 'failed' ? 'Failed' : 'Idle',
                  statusKind === 'failed' ? 'danger' : (statusKind || 'muted'));
    }

    // ---- Input capture (phase 5) ---------------------------------------------------------
    function sendInput(obj) {
        if (controlChannel && controlChannel.readyState === 'open') {
            try { controlChannel.send(JSON.stringify(obj)); } catch (e) { /* dropped frame of input */ }
        }
    }

    // Normalised (0..1) position within the video. The video's default object-fit stretches the
    // stream to the element box, so element-relative coords map straight to capture coords.
    function normPos(e) {
        const r = els.video.getBoundingClientRect();
        return [
            Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)),
            Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)),
        ];
    }

    function wireInput() {
        const v = els.video;
        v.tabIndex = 0;   // make it focusable so it can receive key events
        let lastMove = 0;

        v.addEventListener('mousemove', (e) => {
            const now = performance.now();
            if (now - lastMove < MOVE_THROTTLE_MS) return;
            lastMove = now;
            const [x, y] = normPos(e);
            sendInput({ t: 'm', x, y });
        });
        v.addEventListener('mousedown', (e) => {
            v.focus();
            const [x, y] = normPos(e);
            sendInput({ t: 'd', b: e.button, x, y });
            e.preventDefault();
        });
        v.addEventListener('mouseup', (e) => {
            const [x, y] = normPos(e);
            sendInput({ t: 'u', b: e.button, x, y });
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

    wireInput();

    els.start.addEventListener('click', start);
    els.stop.addEventListener('click', stop);
    els.cad.addEventListener('click', () => sendInput({ t: 'cad' }));
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
