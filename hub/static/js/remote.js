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
//
// WHEN WEBRTC CANNOT CONNECT AT ALL, the session falls back to the hub relay (remote-relay.js,
// hub/remote_relay.py): the agent's frames come down a long-poll and are decoded here, input
// goes up as JSON. It is a fallback rather than a choice because it costs hub bandwidth and
// latency and lets the hub see the picture, so it is only tried once WebRTC has actually
// failed -- or has spent RELAY_AFTER_MS without connecting, since ICE can sit in "checking"
// far longer than anyone will watch a black rectangle. Both ends must agree: the agent says it
// can relay in its offer, and a page without remote-relay.js (Sharing) never asks.
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
    // How long WebRTC gets, from the agent's offer, before the viewer gives up on it and asks
    // for the hub relay. SIPSorcery's own ICE timeout is about sixteen seconds; this is a
    // little longer so that a slow-but-working relay candidate still wins.
    const RELAY_AFTER_MS = 20000;

    // Presets exist because "15fps / 4000kbps / 100%" means nothing to someone who just wants
    // the screen to stop stuttering. Custom reveals the raw numbers for when it does matter.
    const PRESETS = {
        quality:  { fps: 25, bitrate_kbps: 8000, scale: 100 },
        balanced: { fps: 15, bitrate_kbps: 4000, scale: 100 },
        speed:    { fps: 10, bitrate_kbps: 1500, scale: 50 },
    };

    /** m:ss, for the recording pill and its countdown. */
    function clock(seconds) {
        const total = Math.max(0, Math.floor(seconds));
        return Math.floor(total / 60) + ':' + String(total % 60).padStart(2, '0');
    }

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

        // Every hub URL this viewer touches, in one table, so a caller can point it
        // somewhere else. `options.routes` is what makes a BORROWED machine viewable
        // (roadmap #15): the same WebRTC dance, but signalled through this hub's proxy to
        // the hub that owns the PC. Everything below is unchanged by that -- the browser is
        // still the answerer, the agent is still the offerer, and neither knows there is an
        // extra hop, because there is nothing in the signalling that names a hub.
        //
        // A borrowed viewer sets `inventory`, `inventoryRefresh` and `virtualDisplay` to
        // NULL rather than omitting them, and the three callers below check before firing.
        // Omitting would leave the defaults in place, which point at this hub's own remote
        // endpoints for a hostname that is not in its fleet -- the wrong question rather
        // than a failing one. There is no proxy for any of the three on purpose: the
        // session list is hub A's to enumerate, and installing a display driver puts a
        // publisher into somebody else's certificate store, which is not what a share is.
        const routes = Object.assign({
            start: () => `/api/remote/${encodeURIComponent(machine)}/start`,
            signal: (id) => `/api/remote/session/${encodeURIComponent(id)}/signal`,
            poll: (id, seq) =>
                `/api/remote/session/${encodeURIComponent(id)}/poll?after_seq=${seq}`,
            stop: (id) => `/api/remote/session/${encodeURIComponent(id)}/stop`,
            inventory: () => `/api/remote/${encodeURIComponent(machine)}/inventory`,
            inventoryRefresh: () =>
                `/api/remote/${encodeURIComponent(machine)}/inventory/refresh`,
            virtualDisplay: () => `/api/remote/${encodeURIComponent(machine)}/virtual-display`,
            relayDown: (id, after) =>
                `/api/remote/session/${encodeURIComponent(id)}/relay/down?after=${after}`,
            relayUp: (id) => `/api/remote/session/${encodeURIComponent(id)}/relay/up`,
        }, options.routes || {});

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
            record: q('record'),
            recPill: q('rec-pill'),
            recPillText: q('rec-pill-text'),
            recBar: q('rec-bar'),
            recBarText: q('rec-bar-text'),
            recExtend: q('rec-extend'),
            recStop: q('rec-stop'),
            recDialog: q('rec-dialog'),
            recReason: q('rec-reason'),
            recError: q('rec-error'),
            recConfirm: q('rec-confirm'),
            recCancel: q('rec-cancel'),
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
        // Which KINDS of ICE candidate each side managed to gather (host / srflx / relay). The
        // only thing that explains a failed connection from the operator's chair, and the one
        // piece of it that is not in any log: "neither side produced a relay candidate" means the
        // TURN server was unreachable from both, which is a deployment answer, not a bug report.
        let iceTypes = { local: new Set(), remote: new Set() };
        // The hub relay, once this session has fallen back to it (see the file header).
        // `relayCapable` comes from the agent's offer; an agent too old to relay never says so.
        let relay = null;
        let relaySwitching = false;
        let relayCapable = false;
        let relayTimer = null;
        let startedCodec = 'h264';

        function setStatus(text, kind) {
            const state = kind || 'muted';
            els.status.className = 'status-pill status-pill--' + state;
            els.status.innerHTML = '<span class="status-pill__dot"></span>';
            els.status.append(text);
            els.overlayStatus.textContent = text;
            // Recording needs a picture to record, so the button wakes up with "Live" -- and
            // stays up while a recording runs, because it is also how that recording stops.
            if (recorder) els.record.disabled = state !== 'ok' && !recorder.isActive();
            if (options.onStatus) options.onStatus(state, text);
        }

        function hint(text) { els.hint.textContent = text || ''; }
        function meta(text) { els.meta.textContent = text || ''; }

        // A borrowed machine (roadmap #15) is shown the same viewer with two controls taken
        // off it. Neither is a security boundary -- the owning hub has no proxy route for
        // either, so both would simply fail -- but a button that always fails is worse than
        // no button, and "install a display driver on a colleague's PC" is not a thing to
        // offer and then refuse. The `vdd` panel starts hidden in the partial and is only
        // revealed by renderDisplays, which a borrowed viewer never reaches.
        if (options.borrowed) {
            els.refreshSessions.hidden = true;
        }

        // ---- Session recording (roadmap #19) ----------------------------------------------
        // Offered only where it can work and is allowed: never for a borrowed PC (its owning
        // hub has no recording route for a peer, and the share was not granted for one), and
        // only in a browser that can record. Everything else is remote-recorder.js.
        const recorder = (!options.borrowed && window.RemoteRecorder?.supported())
            ? window.RemoteRecorder.create({
                machine,
                sessionId: () => sessionId,
                stream: () => els.video.srcObject,
                onState: onRecordingState,
                onCountdown: onRecordingCountdown,
            })
            : null;
        els.record.hidden = !recorder;

        function onRecordingState(state, detail) {
            const active = state === 'starting' || state === 'recording' || state === 'stopping';
            els.record.textContent = active ? t('recordings.stop_recording') : t('recordings.record');
            els.record.classList.toggle('btn--danger', active);
            els.record.disabled = state === 'stopping' || (!active && !running);
            els.recPill.hidden = !active;
            if (state === 'starting') {
                els.recPillText.textContent = t('recordings.pill_starting');
                hint(t('recordings.starting'));
            } else if (state === 'recording') {
                els.recPillText.textContent = t('recordings.pill', { time: clock(detail.seconds) });
                if (els.hint.textContent === t('recordings.starting')) hint('');
            } else if (state === 'ended') {
                hint(detail.reason && detail.reason !== 'stopped'
                    ? t('recordings.ended_with_reason',
                        { reason: window.RemoteRecorder.endReasonText(detail.reason) })
                    : t('recordings.saved'));
            } else if (state === 'failed') {
                hint(t('recordings.failed', { error: detail.error || '' }));
            }
            if (!active) els.recBar.hidden = true;
        }

        function onRecordingCountdown(secondsLeft) {
            els.recBar.hidden = secondsLeft === null;
            if (secondsLeft !== null) {
                els.recBarText.textContent = t('recordings.countdown', { time: clock(secondsLeft) });
            }
        }

        function openRecordingDialog() {
            els.recReason.value = '';
            els.recError.hidden = true;
            els.recDialog.showModal();
            els.recReason.focus();
        }

        function confirmRecording() {
            const reason = els.recReason.value.trim();
            if (!reason) {
                els.recError.textContent = t('recordings.reason_required');
                els.recError.hidden = false;
                els.recReason.focus();
                return;
            }
            els.recDialog.close();
            recorder.start(reason);
        }

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
            iceTypes = { local: new Set(), remote: new Set() };
            relayCapable = false;
            startedCodec = els.codec.value;
            try {
                const body = Object.assign({
                    session: els.session.value || 'auto',
                    codec: els.codec.value,
                    encoder: els.encoder.value,
                }, liveSettings());
                const res = await window.FleetApi.postJson(
                    routes.start(), body);
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
                // Ask for the shortest jitter buffer the browser will give us. Chrome's default
                // tuning is for WATCHING video: it holds 100-200ms of frames back so that network
                // jitter never shows up as a stutter. That is the right trade for a video call and
                // the wrong one here -- every millisecond it buffers is a millisecond between the
                // operator moving the mouse and seeing the pointer move, and it dwarfs anything
                // the capture and encode side costs. Remote control would far rather stutter than
                // lag, so we ask for 0 and let the browser clamp it to whatever it can actually
                // sustain (it treats this as a hint, not a promise).
                //
                // Chromium-only. Firefox and Safari expose no equivalent and simply will not have
                // the property, so this is a no-op there rather than an error -- their operators
                // keep the latency they had before, which is why this is set defensively and
                // nothing downstream depends on it having worked.
                if (e.receiver && 'playoutDelayHint' in e.receiver) {
                    try { e.receiver.playoutDelayHint = 0; } catch (err) { /* not writable here */ }
                }
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
                if (c.type) iceTypes.local.add(c.type);
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
                        clearRelayTimer();
                        setStatus(t('machine.remote.live'), 'ok'); hint(''); break;
                    case 'disconnected':
                        setStatus(t('machine.remote.reconnecting'), 'warn'); break;
                    case 'failed':
                        if (tryRelay('failed')) break;
                        hint(iceDiagnosis() + relayUnavailableNote());
                        teardown('failed');
                        break;
                    case 'closed': break;
                }
            };
        }

        /** Why the connection failed, in terms of what ICE actually had to work with.
         *
         * "Connection failed." on its own sends the operator to the agent log, which will say
         * the same thing from the other side. What decides the case is which candidate types
         * each end produced: no relay candidate anywhere means the TURN server was not reachable
         * from either machine (firewall, port forwarding, or a relay URL that only resolves
         * inside the hub's network), which is a different problem from one side relaying fine
         * and the pair still failing.
         */
        function iceDiagnosis() {
            const list = (set) => (set.size ? Array.from(set).sort().join(', ') : '-');
            const detail = t('machine.remote.ice_summary', {
                local: list(iceTypes.local), remote: list(iceTypes.remote),
            });
            const noRelay = !iceTypes.local.has('relay') && !iceTypes.remote.has('relay');
            return t('machine.remote.connection_failed') + ' ' + detail +
                   (noRelay ? ' ' + t('machine.remote.ice_no_relay') : '');
        }

        // ---- Hub relay fallback -----------------------------------------------------------
        function canRelay() {
            return running && !relay && !relaySwitching && relayCapable &&
                   !!routes.relayDown && !!routes.relayUp &&
                   !!window.RemoteRelay && window.RemoteRelay.supported();
        }

        // Why the fallback was not tried, when it would have been. Only the browser case is
        // worth a sentence: an old agent or a page that cannot relay is not the operator's to
        // fix from here.
        function relayUnavailableNote() {
            if (relayCapable && routes.relayDown && window.RemoteRelay &&
                    !window.RemoteRelay.supported()) {
                return ' ' + t('machine.remote.relay_unsupported');
            }
            return '';
        }

        function armRelayTimer() {
            clearRelayTimer();
            if (!canRelay()) return;
            relayTimer = setTimeout(() => {
                relayTimer = null;
                if (pc && pc.connectionState !== 'connected') tryRelay('timeout');
            }, RELAY_AFTER_MS);
        }

        function clearRelayTimer() {
            if (relayTimer) { clearTimeout(relayTimer); relayTimer = null; }
        }

        /** Give up on WebRTC and carry the session through the hub. Returns true if the
         *  fallback was started (or already is), false if it is not available here -- in which
         *  case the caller keeps the old behaviour and reports the failure. */
        function tryRelay(reason) {
            clearRelayTimer();
            if (relay || relaySwitching) return true;
            if (!canRelay()) return false;
            relaySwitching = true;
            setStatus(t('machine.remote.connecting'), 'warn');
            hint(t('machine.remote.relay_switching'));
            // The peer can only get in the way from here: a late ICE success would start a
            // second stream into the same <video>. Detach its handlers before closing it so
            // its 'closed' transition does not run the failure path again.
            if (pc) {
                pc.onconnectionstatechange = null;
                pc.ontrack = null;
                try { pc.close(); } catch (e) { /* already closed */ }
                pc = null;
            }
            controlChannel = null;
            const id = sessionId;
            window.FleetApi.postJson(routes.signal(id), { kind: 'relay', payload: { reason } })
                .then(() => {
                    relaySwitching = false;
                    if (!running || sessionId !== id) return;
                    relay = window.RemoteRelay.create({
                        video: els.video,
                        codec: startedCodec,
                        downUrl: (after) => routes.relayDown(id, after),
                        upUrl: () => routes.relayUp(id),
                        onControl: handleAgentStatus,
                        onFirstFrame: () => setStatus(t('machine.remote.live_relay'), 'ok'),
                        onClosed: () => {
                            if (sessionId !== id) return;
                            hint(t('machine.remote.session_ended'));
                            teardown('muted');
                        },
                    });
                    els.cad.disabled = false;
                    // Same as the DataChannel's onopen: the agent started with what the hub
                    // queued, and the operator may have changed a control since.
                    sendConfig();
                    schedulePoll();
                })
                .catch((e) => {
                    relaySwitching = false;
                    if (sessionId !== id) return;
                    hint(t('machine.remote.relay_failed', { error: e.message }));
                    teardown('failed');
                });
            return true;
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
                routes.signal(sessionId), { kind, payload }
            ).catch(() => { /* transient; the next tick retries the relevant state */ });
        }

        function schedulePoll() {
            if (!running) return;
            if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
            // Relayed counts as connected: the media has its own long-poll, and this one is
            // only waiting for the session to end.
            const connected = !!relay || pc?.connectionState === 'connected';
            pollTimer = setTimeout(poll, connected ? POLL_CONNECTED_MS : POLL_INTERVAL_MS);
        }

        async function poll() {
            if (!running || !sessionId) return;
            try {
                const res = await window.FleetApi.getJson(
                    routes.poll(sessionId, afterSeq));
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
            if (sig.kind === 'bye') { teardown('muted'); return; }
            if (!pc) return;
            try {
                if (sig.kind === 'offer') {
                    relayCapable = !!sig.payload?.relay;
                    await pc.setRemoteDescription({ type: 'offer', sdp: sig.payload.sdp });
                    remoteSet = true;
                    for (const ice of pendingIce) await pc.addIceCandidate(ice).catch(() => {});
                    pendingIce = [];
                    const answer = await pc.createAnswer();
                    await pc.setLocalDescription(answer);
                    await postSignal('answer', { type: 'answer', sdp: answer.sdp });
                    armRelayTimer();
                } else if (sig.kind === 'ice') {
                    // RTCIceCandidate.type is only populated on candidates WE created, so the
                    // agent's type is read off the SDP line: "... <ip> <port> typ <type> ...".
                    const typed = /\btyp\s+(\w+)/.exec(sig.payload.candidate || '');
                    if (typed) iceTypes.remote.add(typed[1]);
                    const cand = {
                        candidate: sig.payload.candidate,
                        sdpMid: sig.payload.sdpMid,
                        sdpMLineIndex: sig.payload.sdpMLineIndex,
                    };
                    if (remoteSet) await pc.addIceCandidate(cand).catch(() => {});
                    else pendingIce.push(cand);
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
            return fetch(routes.stop(id), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: '{}',
                keepalive,
            }).catch(() => { /* best effort; the hub's TTL sweep is the backstop */ });
        }

        async function stop() {
            const id = sessionId;
            // A recording is flushed BEFORE the session goes: once the session has ended, the
            // hub ends the recording with it and refuses the chunk still in the browser.
            if (recorder?.isActive()) await recorder.stop('stopped');
            teardown('muted');
            await stopSession(id);
        }

        function teardown(statusKind) {
            running = false;
            // Any other way the session ends (the PC, the hub, an expired TTL) ends the
            // recording at the hub too; this only stops the recorder feeding a dead stream.
            if (recorder?.isActive()) recorder.stop('stopped');
            if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
            clearRelayTimer();
            if (relay) { relay.stop(); relay = null; }
            relaySwitching = false;
            relayCapable = false;
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
            if (relay) { relay.send(obj); return; }
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
            // A route set to null means "this hub cannot answer that about this machine",
            // which is the borrowed case: there is no proxy for the session list, and
            // falling back to the LOCAL endpoint would ask this hub about a hostname that
            // is not in its fleet. Skipping leaves the picker on "Auto", which is exactly
            // what an agent too old to report its sessions already leaves it on.
            if (!routes.inventory) return;
            let data;
            try {
                data = await window.FleetApi.getJson(routes.inventory());
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
            if (!routes.inventoryRefresh) return;
            els.refreshSessions.disabled = true;
            hint(t('machine.remote.refreshing'));
            try {
                await window.FleetApi.postJson(routes.inventoryRefresh(), {});
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
            if (!routes.virtualDisplay) return;
            const button = mode === 'install' ? els.vddInstall : els.vddUninstall;
            button.disabled = true;
            hint(mode === 'install' ? t('machine.remote.vdd_queuing_install')
                                    : t('machine.remote.vdd_queuing_remove'));
            try {
                await window.FleetApi.postJson(
                    routes.virtualDisplay(),
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
            // Same order as stop(): the recording's last chunk first, then the session.
            const flushed = recorder?.isActive()
                ? recorder.stop('stopped') : Promise.resolve();
            flushed.finally(() => stopSession(id));
            teardown('muted');
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
        if (recorder) {
            els.record.addEventListener('click', () => {
                if (recorder.isActive()) recorder.stop('stopped');
                else openRecordingDialog();
            });
            els.recConfirm.addEventListener('click', confirmRecording);
            els.recCancel.addEventListener('click', () => els.recDialog.close());
            els.recReason.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) confirmRecording();
            });
            els.recExtend.addEventListener('click', () => recorder.extend());
            els.recStop.addEventListener('click', () => recorder.stop('stopped'));
        }

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

    // The only entry point: the Remote page builds a viewer per open PC from its <template>
    // copy of the partial (remote-workspace.js). There is no self-bootstrapping viewer here
    // any more -- the machine page used to include the partial directly and have this file
    // adopt it, which is the second entry point that let one PC be opened twice.
    window.RemoteViewer = { create };
})();
