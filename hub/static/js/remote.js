// Remote view/control (roadmap #2). The console side of the WebRTC session: it starts a
// session, answers the agent helper's offer, renders the incoming video, and sends input and
// live quality changes back over the agent-created "control" DataChannel.
//
// The agent is the offerer (it has the media), so the browser is the ANSWERER: it polls the
// hub for the agent's offer + trickled ICE, answers, and trickles its own ICE back. Signaling
// is plain HTTP polling through /api/remote/* -- same model as the fleet terminal, and the hub
// relays between the two sides (see remote_web.py).
//
// ONE VIEWER PER PC, AND SEVERAL AT ONCE. This file is a FACTORY (window.RemoteViewer.create)
// rather than a page script, because "remote into two machines at the same time" is ordinary
// helpdesk work -- one PC to read an error off, another to fix it on -- and the hub has always
// allowed it: sessions are keyed by machine, each lives on its own agent, and nothing in
// remote.py serialises them. It was only this file that could hold one session at a time,
// because it addressed its controls by getElementById.
//
// So: every viewer gets a ROOT element (the partial in templates/partials/_remote_viewer.html)
// and looks its controls up inside it, keeps all of its state in the closure, and never
// touches a global. Two callers use that:
//   * the machine page, which has exactly one viewer for the PC it is about -- bootstrapped at
//     the bottom of this file, since a page with one viewer should not need a script to say so;
//   * the Remote page (remote-workspace.js), which clones the partial once per open PC and
//     keeps every session live in the background so switching tabs is instant.
//
// Two classes of setting, and the split is not cosmetic:
//   * START-TIME (Windows session, codec, encoder) is negotiated in the SDP or decides which
//     encoder object the agent builds, so changing it requires a new session. The UI disables
//     those controls while connected rather than letting a change silently do nothing.
//   * LIVE (monitor, fps, bitrate, scale) rides the control channel as a {t:'cfg'} message and
//     the agent rebuilds its capture pipeline in place.
(function () {
    'use strict';

    // Signaling cadence. Fast while the session is being set up, because every tick is a
    // round of trickled ICE the connection is waiting on; slow once media is flowing, because
    // the only things left to arrive are a late candidate for a better path and the eventual
    // 'bye'. That difference is what makes four open screens cost about one screen's worth of
    // polling: at the setup rate they would be 5 requests a second between them, against a
    // hub whose thread pool is fixed (see fleet-pty.js for the same trade on consoles).
    const POLL_INTERVAL_MS = 800;
    const POLL_CONNECTED_MS = 3000;
    const MOVE_THROTTLE_MS = 40;   // ~25 mouse-move messages/sec is plenty and won't flood

    // Presets exist because "15fps / 4000kbps / 100%" means nothing to someone who just wants
    // the screen to stop stuttering. Custom reveals the raw numbers for when it does matter.
    const PRESETS = {
        quality:  { fps: 25, bitrate_kbps: 8000, scale: 100 },
        balanced: { fps: 15, bitrate_kbps: 4000, scale: 100 },
        speed:    { fps: 10, bitrate_kbps: 1500, scale: 50 },
    };

    function clampInt(value, low, high, fallback) {
        const n = parseInt(value, 10);
        if (!Number.isFinite(n)) return fallback;
        return Math.min(high, Math.max(low, n));
    }

    /** Wire `root` (a clone or include of _remote_viewer.html) up to `machine`.
     *
     *  opts.autoStart  connect immediately instead of waiting for the Start button. The
     *                  Remote page passes this: picking a PC out of the "open a screen"
     *                  dialog IS the decision to connect to it, and a tab that opens onto a
     *                  black rectangle with a Start button is a second click for nothing.
     *  opts.onStatus   (kind, text) whenever the connection state changes, so a caller that
     *                  renders the viewer somewhere collapsed -- a tab -- can show it there.
     */
    function create(root, machine, opts) {
        const options = opts || {};
        const q = (name) => root.querySelector(`[data-remote="${name}"]`);

        const els = {
            title: q('title'),
            start: q('start'),
            stop: q('stop'),
            cad: q('cad'),
            status: q('status'),
            video: q('video'),
            stage: q('stage'),
            overlay: q('overlay'),
            overlayStatus: q('overlay-status'),
            exitFullscreen: q('exit-fullscreen'),
            hint: q('hint'),
            meta: q('meta'),
            desktopBadge: q('desktop-badge'),
            headlessBadge: q('headless-badge'),
            session: q('session'),
            refreshSessions: q('refresh-sessions'),
            codec: q('codec'),
            encoder: q('encoder'),
            monitor: q('monitor'),
            preset: q('preset'),
            fps: q('fps'),
            bitrate: q('bitrate'),
            scale: q('scale'),
            viewOnly: q('viewonly'),
            fullscreen: q('fullscreen'),
            vdd: q('vdd'),
            vddText: q('vdd-text'),
            vddInstall: q('vdd-install'),
            vddUninstall: q('vdd-uninstall'),
        };

        let pc = null;
        let controlChannel = null;
        let sessionId = null;
        let afterSeq = 0;
        let pollTimer = null;
        let remoteSet = false;
        let pendingIce = [];
        let running = false;
        let disposed = false;
        // Capture geometry as last reported by the agent over the control channel. Needed to
        // map pointer coordinates correctly once the video is letterboxed (object-fit: contain).
        let captured = { w: 0, h: 0 };

        function setStatus(text, kind) {
            const state = kind || 'muted';
            els.status.className = 'status-pill status-pill--' + state;
            els.status.innerHTML = '<span class="status-pill__dot"></span>';
            els.status.append(text);
            els.overlayStatus.textContent = text;
            if (options.onStatus) options.onStatus(state, text);
        }

        function hint(text) { els.hint.textContent = text || ''; }
        function meta(text) { els.meta.textContent = text || ''; }

        // ---- Start-time settings ---------------------------------------------------------
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

        // Keep the custom fields in step with the chosen preset, so switching to Custom starts
        // from what you were just watching rather than from stale defaults.
        function syncPresetFields() {
            const custom = els.preset.value === 'custom';
            root.querySelectorAll('.remote-field--custom').forEach((el) => { el.hidden = !custom; });
            if (!custom) {
                const preset = PRESETS[els.preset.value];
                if (preset) {
                    els.fps.value = preset.fps;
                    els.bitrate.value = preset.bitrate_kbps;
                    els.scale.value = String(preset.scale);
                }
            }
        }

        // ---- Session lifecycle -----------------------------------------------------------
        async function start() {
            if (running || disposed) return;
            running = true;
            els.start.disabled = true;
            els.stop.disabled = false;
            lockStartTimeControls(true);
            setStatus(t('machine.remote.starting'), 'warn');
            hint(t('machine.remote.waiting_helper'));
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
                    `/api/remote/${encodeURIComponent(machine)}/start`, body);
                if (disposed) {   // the tab was closed while the start was in flight
                    stopSession(res.session_id);
                    return;
                }
                sessionId = res.session_id;
                createPeer(res.ice_servers || []);
                schedulePoll();
            } catch (e) {
                hint(t('machine.remote.start_failed', { error: e.message }));
                teardown('failed');
            }
        }

        function createPeer(iceServers) {
            pc = new RTCPeerConnection({ iceServers });

            // The agent offers a send-only track with NO a=msid (SIPSorcery does not emit one,
            // and nothing on the agent side sets a stream id). Chrome honours that literally:
            // ontrack fires with an EMPTY e.streams, so keying off e.streams[0] alone leaves
            // srcObject null and the operator gets a permanently blank stage -- while the data
            // channel works fine, so input and status still behave and the session looks
            // healthy from both ends. Build a MediaStream from the bare track instead. Verified
            // against a live session 2026-07-28.
            pc.ontrack = (e) => {
                if (e.streams && e.streams[0]) {
                    els.video.srcObject = e.streams[0];
                    return;
                }
                // Add the track BEFORE assigning srcObject: assigning an empty MediaStream and
                // mutating it afterwards does not reliably start playback in every browser.
                const stream = els.video.srcObject instanceof MediaStream
                    ? els.video.srcObject : new MediaStream();
                if (!stream.getTracks().includes(e.track)) stream.addTrack(e.track);
                if (els.video.srcObject !== stream) els.video.srcObject = stream;
                // The element is muted + autoplay so this should not be needed, but a rejected
                // play() is worth ignoring rather than throwing inside an event handler.
                const played = els.video.play();
                if (played && played.catch) played.catch(() => {});
            };
            // The agent (offerer) creates the "control" channel. It carries input UP and status
            // (geometry, desktop switches, capture stalls) DOWN.
            pc.ondatachannel = (e) => {
                if (e.channel.label !== 'control') return;
                controlChannel = e.channel;
                controlChannel.onopen = () => {
                    els.cad.disabled = false;
                    // Re-assert the live settings: the agent started with what the hub queued,
                    // but the operator may have changed a control while we were connecting.
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
                    case 'connecting':
                        setStatus(t('machine.remote.connecting'), 'warn'); break;
                    case 'connected':
                        setStatus(t('machine.remote.live'), 'ok'); hint(''); break;
                    case 'disconnected':
                        setStatus(t('machine.remote.reconnecting'), 'warn'); break;
                    case 'failed':
                        hint(t('machine.remote.connection_failed')); teardown('failed'); break;
                    case 'closed': break;
                }
            };
        }

        // Status the agent pushes down the control channel. Without this, "the screen went
        // black" is unguessable from here -- and the agent already knows why.
        function handleAgentStatus(raw) {
            let msg;
            try { msg = JSON.parse(raw); } catch (e) { return; }
            if (!msg || typeof msg !== 'object') return;

            if (msg.t === 'geom') {
                captured = { w: msg.w || 0, h: msg.h || 0 };
                const secure = msg.desktop && msg.desktop.toLowerCase() !== 'default';
                els.desktopBadge.hidden = !secure;
                if (secure) {
                    // 'Winlogon' is the desktop OBJECT's name, not prose -- any other value is
                    // shown verbatim because it came from Windows.
                    els.desktopBadge.lastChild.textContent =
                        msg.desktop === 'Winlogon'
                            ? t('machine.remote.logon_screen_desktop') : msg.desktop;
                }
                populateMonitors(msg.monitors, msg.monitor);
                meta(t('machine.remote.geom', {
                    width: msg.w, height: msg.h, encoder: msg.encoder || '',
                    desktop: msg.desktop || t('machine.remote.desktop_unknown'),
                }));
                hint('');
            } else if (msg.t === 'capture' && msg.state === 'stalled') {
                // Two whole sentences rather than one spliced around an optional clause:
                // "on the X desktop" cannot be dropped into the middle of a translated
                // sentence and still read as a sentence.
                hint(msg.desktop
                    ? t('machine.remote.stalled_on_desktop', { desktop: msg.desktop })
                    : t('machine.remote.stalled'));
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
            const connected = pc && pc.connectionState === 'connected';
            pollTimer = setTimeout(poll, connected ? POLL_CONNECTED_MS : POLL_INTERVAL_MS);
        }

        async function poll() {
            if (!running || !sessionId) return;
            try {
                const res = await window.FleetApi.getJson(
                    `/api/remote/session/${encodeURIComponent(sessionId)}/poll?after_seq=${afterSeq}`);
                afterSeq = res.next_seq;
                for (const sig of res.signals || []) await handleSignal(sig);
                if (res.status === 'ended' || res.status === 'expired') {
                    hint(res.status === 'expired'
                        ? t('machine.remote.session_expired') : t('machine.remote.session_ended'));
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
                hint(t('machine.remote.signaling_error', { error: e.message }));
            }
        }

        /** End a session on the hub. Split out from stop() so it can also retire a session
         *  this viewer started but no longer owns -- one whose start landed after the tab was
         *  closed. keepalive so it still goes out from a pagehide handler. */
        function stopSession(id, { keepalive = false } = {}) {
            if (!id) return Promise.resolve();
            return fetch(`/api/remote/session/${encodeURIComponent(id)}/stop`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: '{}',
                keepalive,
            }).catch(() => { /* best effort; the hub's TTL sweep is the backstop */ });
        }

        async function stop() {
            const id = sessionId;
            teardown('muted');
            await stopSession(id);
        }

        function teardown(statusKind) {
            running = false;
            if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
            if (pc) { try { pc.close(); } catch (e) {} pc = null; }
            controlChannel = null;
            if (els.video.srcObject) {
                els.video.srcObject.getTracks().forEach((track) => track.stop());
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
            setStatus(statusKind === 'failed' ? t('machine.remote.failed')
                                              : t('machine.remote.idle'),
                      statusKind === 'failed' ? 'danger' : (statusKind || 'muted'));
        }

        // ---- Live configuration ----------------------------------------------------------
        function sendConfig() {
            const settings = liveSettings();
            sendControl(Object.assign({ t: 'cfg' }, settings));
        }

        function sendControl(obj) {
            if (controlChannel && controlChannel.readyState === 'open') {
                try { controlChannel.send(JSON.stringify(obj)); } catch (e) { /* dropped */ }
            }
        }

        // ---- Input capture ---------------------------------------------------------------
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
        // element coordinates straight through would put every click off by the size of the
        // bars. Returns null for a click in the letterbox, which is not on the remote desktop
        // at all.
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
            // Only intercept keys while the video is focused, so the operator can still use the
            // rest of the page normally -- and, with several screens open, so a keystroke goes
            // to the PC whose picture was last clicked rather than to all of them.
            v.addEventListener('keydown', (e) => {
                sendInput({ t: 'k', code: e.code, key: e.key, down: true });
                e.preventDefault();
            });
            v.addEventListener('keyup', (e) => {
                sendInput({ t: 'k', code: e.code, key: e.key, down: false });
                e.preventDefault();
            });
        }

        // ---- Inventory: session picker + headless badge -----------------------------------
        async function loadInventory() {
            let data;
            try {
                data = await window.FleetApi.getJson(
                    `/api/remote/${encodeURIComponent(machine)}/inventory`);
            } catch (e) {
                return;   // an agent too old to report leaves the picker on "Auto", which works
            }
            if (disposed) return;
            renderSessions(data.sessions || []);
            renderDisplays(data.displays || {}, data.payload_available);
        }

        function renderSessions(sessions) {
            const selected = els.session.value;
            els.session.innerHTML = '';
            els.session.appendChild(new Option(t('machine.remote.session_auto'), 'auto'));
            for (const s of sessions) {
                // A session with nobody signed in is the logon screen -- and on a headless
                // machine that is exactly the one the operator needs, so it is labelled, not
                // hidden.
                const who = s.is_logon_screen
                    ? t('machine.remote.session_logon_screen')
                    : (s.account || t('machine.remote.session_no_user'));
                // s.state is a Windows session state (Active/Disconnected/...) reported
                // verbatim by the agent, so it stays as it came.
                const bits = [s.state];
                if (s.is_console) bits.push(t('machine.remote.session_console'));
                if (s.client) bits.push(s.client);
                els.session.appendChild(new Option(
                    t('machine.remote.session_option',
                      { id: s.id, who, details: bits.join(', ') }),
                    String(s.id)));
            }
            // Keep the operator's choice across refreshes when it still exists.
            els.session.value =
                Array.from(els.session.options).some((o) => o.value === selected) ? selected : 'auto';
        }

        function renderDisplays(displays, payloadAvailable) {
            const headless = !!displays.headless;
            const present = !!displays.virtual_display_present;
            els.headlessBadge.hidden = !headless;

            // The panel is shown when there is a decision to make: nothing to capture
            // (install), or a virtual display already here (remove it once a real monitor
            // shows up).
            els.vdd.hidden = !(headless || present);
            els.vddInstall.hidden = present;
            els.vddUninstall.hidden = !present;

            if (present) {
                els.vddText.textContent = displays.virtual_display_started
                    ? t('machine.remote.vdd_present',
                        { monitors: displays.physical_monitors })
                    : t('machine.remote.vdd_present_stopped',
                        { monitors: displays.physical_monitors });
            } else if (headless) {
                els.vddText.textContent = payloadAvailable
                    ? t('machine.remote.vdd_headless_ready')
                    : t('machine.remote.vdd_headless_no_payload');
                els.vddInstall.disabled = !payloadAvailable;
            }
        }

        async function refreshInventory() {
            els.refreshSessions.disabled = true;
            hint(t('machine.remote.refreshing'));
            try {
                await window.FleetApi.postJson(
                    `/api/remote/${encodeURIComponent(machine)}/inventory/refresh`, {});
                // The agent picks the command up on its next poll and answers on its next
                // heartbeat, so give it a beat before reading back rather than showing stale
                // data.
                setTimeout(() => {
                    if (disposed) return;
                    loadInventory();
                    hint('');
                }, 4000);
            } catch (e) {
                hint(t('machine.remote.refresh_failed', { error: e.message }));
            } finally {
                setTimeout(() => {
                    if (!disposed) els.refreshSessions.disabled = running;
                }, 4000);
            }
        }

        async function virtualDisplay(mode) {
            const button = mode === 'install' ? els.vddInstall : els.vddUninstall;
            button.disabled = true;
            hint(mode === 'install' ? t('machine.remote.vdd_queuing_install')
                                    : t('machine.remote.vdd_queuing_remove'));
            try {
                await window.FleetApi.postJson(
                    `/api/remote/${encodeURIComponent(machine)}/virtual-display`,
                    { mode, monitors: 1, resolutions: [{ width: 1920, height: 1080, hz: 60 }] });
                hint(t('machine.remote.vdd_queued'));
                setTimeout(() => { if (!disposed) loadInventory(); }, 15000);
            } catch (e) {
                hint(t('machine.remote.vdd_queue_failed', { error: e.message }));
            } finally {
                button.disabled = false;
            }
        }

        // ---- Fullscreen ------------------------------------------------------------------
        function toggleFullscreen() {
            if (document.fullscreenElement) {
                document.exitFullscreen().catch(() => {});
            } else {
                els.stage.requestFullscreen().catch(
                    (e) => hint(t('machine.remote.fullscreen_refused', { error: e.message })));
            }
        }

        // Registered on the document (that is where the event fires) but answered per viewer:
        // `full` is false for every viewer except the one whose stage is actually fullscreen,
        // so with several screens open the other tabs' overlays stay hidden.
        function onFullscreenChange() {
            const full = document.fullscreenElement === els.stage;
            els.overlay.hidden = !full;
            els.fullscreen.textContent = full ? t('machine.remote.exit_fullscreen')
                                              : t('machine.remote.fullscreen');
            // Focus the video so keystrokes go to the remote machine rather than the page.
            if (full) els.video.focus();
        }

        // A viewer that goes away with the page still holds a session on the hub, and a
        // session outlives the browser by its TTL -- hours, during which the agent keeps a
        // capture helper up and a TURN credential live. keepalive fetch is what lets the stop
        // leave a page that is already unloading (sendBeacon cannot set the JSON content-type
        // the hub requires, which is why this is not one).
        function onPageHide() {
            if (sessionId) stopSession(sessionId, { keepalive: true });
            if (pc) { try { pc.close(); } catch (e) {} }
        }

        // The other half of that: a page restored from the back/forward cache comes back with
        // its old DOM -- a "Live" pill over the last frame it received -- and a session the
        // handler above already ended. Reset it to Idle so the viewer isn't lying about a
        // connection that no longer exists. (An open RTCPeerConnection usually makes a page
        // ineligible for the bfcache in the first place, so this is the belt to that braces.)
        function onPageShow(e) {
            if (e.persisted && running) teardown('muted');
        }

        /** Drop this viewer: end its session, stop its timers, and unregister the two
         *  listeners it had to put on shared objects. Called when a Remote-page tab is
         *  closed. */
        function dispose() {
            if (disposed) return;
            disposed = true;
            const id = sessionId;
            teardown('muted');
            stopSession(id);
            document.removeEventListener('fullscreenchange', onFullscreenChange);
            window.removeEventListener('pagehide', onPageHide);
            window.removeEventListener('pageshow', onPageShow);
        }

        // ---- Wiring ----------------------------------------------------------------------
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
            hint(els.viewOnly.checked ? t('machine.remote.view_only_note') : '');
        });

        document.addEventListener('fullscreenchange', onFullscreenChange);
        window.addEventListener('pagehide', onPageHide);
        window.addEventListener('pageshow', onPageShow);

        if (options.autoStart) start();

        return {
            machine,
            root,
            start,
            stop,
            dispose,
            isLive: () => running,
            /** Give the remote desktop the keyboard, so a freshly-shown tab can be typed
             *  into without clicking the picture first. */
            focus: () => els.video.focus(),
            /** The Remote page labels each screen with its PC, since a strip of tabs all
             *  reading "Remote view" would be useless. */
            setTitle(text) { els.title.textContent = text; },
        };
    }

    window.RemoteViewer = { create };

    // ---- The machine page's single viewer -------------------------------------------------
    // Not in a DOMContentLoaded handler: page scripts are loaded at the end of <body>, so the
    // markup above is already parsed. The Remote page keeps its copy of the partial inside a
    // <template>, whose content is a separate document fragment -- querySelector does not
    // reach into it -- so this cannot accidentally adopt one of that page's screens.
    const solo = document.querySelector('[data-remote-viewer]');
    if (solo && window.FleetApi && window.FleetApi.machine) {
        create(solo, window.FleetApi.machine);
    }
})();
