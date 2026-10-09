// The hub-relayed half of a remote session (roadmap #2, hub/remote_relay.py): what the viewer
// falls back to when WebRTC cannot find a path at all.
//
// WebRTC gives the browser a decoded <video> track and a DataChannel for free. The relay gives
// it neither -- just the agent's encoded frames over a long-poll -- so this file rebuilds the
// two halves remote.js relies on:
//   * PICTURE: WebCodecs decodes the frames onto a canvas, and the canvas's captureStream() is
//     handed to the SAME <video> element WebRTC would have used. That is the whole reason for
//     the canvas detour rather than drawing on a canvas in the stage: remote.js's pointer
//     mapping (videoWidth/videoHeight, object-fit letterboxing), fullscreen and focus handling
//     all keep working unchanged, so a relayed screen behaves exactly like a direct one.
//   * CONTROL: input and live settings go up in small JSON batches; the agent's status records
//     (geometry, desktop switches, stalls) come down interleaved with the frames and are handed
//     to remote.js's existing handler.
//
// Exposed as window.RemoteRelay.create(...), a factory like RemoteViewer, and loaded only on
// pages that can relay. A page without it (the Sharing page -- a borrowed machine is never
// relayed, see remote_web.peer_signal) simply never offers the fallback.
(function () {
    'use strict';

    // remote_relay.py framing: [kind:1][flags:1][length:4 BE][payload]
    const KIND_VIDEO = 1;
    const KIND_CONTROL = 2;
    const FLAG_KEYFRAME = 0x01;
    const HEADER = 6;

    // If the decoder still has this many frames waiting when the NEXT batch arrives, it is not
    // keeping up: drop deltas until the next keyframe. On a remote-control screen a frame that
    // is two seconds late is worse than no frame -- the operator is clicking on the past.
    //
    // Judged per batch, not per frame. A new viewer's first batch is everything since the
    // newest keyframe -- up to a GOP, submitted in one go -- and a per-frame check read that
    // burst as "behind", threw it away and asked for another keyframe on every single start
    // (seen in the harness run of 2026-10-08).
    const MAX_DECODE_QUEUE = 8;
    const RETRY_MS = 1000;

    function sleep(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }

    /** True if this browser can play a relayed session at all. */
    function supported() {
        return typeof window.VideoDecoder === 'function' &&
               typeof window.EncodedVideoChunk === 'function' &&
               typeof HTMLCanvasElement.prototype.captureStream === 'function';
    }

    /** The WebCodecs codec string for an H.264 stream, read from its SPS rather than assumed.
     *
     * The SDP advertises Constrained Baseline, but that is what the agent ASKS for -- a
     * hardware encoder is free to produce Main or High, and WebRTC's decoder never cared. A
     * VideoDecoder configured with the wrong profile may refuse the stream outright, so the
     * string is built from the profile, constraint and level bytes that follow the SPS NAL
     * header (type 7) in the keyframe itself. Falls back to Constrained Baseline 5.1 when no
     * SPS is found, which is what the stream claims to be. */
    function h264CodecString(bytes) {
        for (let i = 0; i + 4 < bytes.length; i++) {
            // Annex-B start code 00 00 01 (a four-byte 00 00 00 01 ends in the same three).
            if (bytes[i] === 0 && bytes[i + 1] === 0 && bytes[i + 2] === 1) {
                const nalType = bytes[i + 3] & 0x1f;
                if (nalType === 7 && i + 6 < bytes.length) {
                    const hex = (b) => b.toString(16).padStart(2, '0').toUpperCase();
                    return 'avc1.' + hex(bytes[i + 4]) + hex(bytes[i + 5]) + hex(bytes[i + 6]);
                }
            }
        }
        return 'avc1.42E033';
    }

    function parseRecords(buffer) {
        const view = new DataView(buffer);
        const bytes = new Uint8Array(buffer);
        const out = [];
        let offset = 0;
        while (offset + HEADER <= bytes.length) {
            const kind = view.getUint8(offset);
            const flags = view.getUint8(offset + 1);
            const length = view.getUint32(offset + 2);
            offset += HEADER;
            if (offset + length > bytes.length) break;   // truncated: drop the tail
            out.push({ kind, flags, payload: bytes.subarray(offset, offset + length) });
            offset += length;
        }
        return out;
    }

    /** Start relaying one session.
     *
     *  opts.video      the viewer's <video> element; the decoded picture is played into it
     *  opts.codec      'h264' or 'vp8' -- what the session was started with
     *  opts.downUrl    (after) => URL of the frames long-poll
     *  opts.upUrl      () => URL to POST input batches to
     *  opts.onControl  (jsonText) for each status record from the agent
     *  opts.onFirstFrame ()  once the first frame is on screen
     *  opts.onClosed   ()    when the hub says the relay is gone (session ended)
     */
    function create(opts) {
        const canvas = document.createElement('canvas');
        const ctx = canvas.getContext('2d');
        const stream = canvas.captureStream();
        let decoder = null;
        let configured = false;
        let waitingForKey = true;
        let after = 0;
        let stopped = false;
        let shownFirst = false;
        let abort = null;
        // Wall-clock microseconds. The decoder needs increasing timestamps, and the stream's
        // real cadence varies with the agent's capture rate, so "now" is as honest as anything.
        let lastTimestamp = 0;

        const upQueue = [];
        let upBusy = false;

        opts.video.srcObject = stream;
        const played = opts.video.play();
        played?.catch?.(() => {});

        function newDecoder() {
            if (decoder) { try { decoder.close(); } catch (e) { /* already closed */ } }
            configured = false;
            waitingForKey = true;
            decoder = new VideoDecoder({
                output: (frame) => {
                    if (canvas.width !== frame.displayWidth || canvas.height !== frame.displayHeight) {
                        canvas.width = frame.displayWidth;
                        canvas.height = frame.displayHeight;
                    }
                    ctx.drawImage(frame, 0, 0);
                    frame.close();
                    if (!shownFirst) {
                        shownFirst = true;
                        if (opts.onFirstFrame) opts.onFirstFrame();
                    }
                },
                // A decode error leaves the decoder closed. Build a new one and wait for the
                // next keyframe -- and ask for one rather than waiting out the agent's GOP.
                error: () => {
                    if (stopped) return;
                    newDecoder();
                    send({ t: 'key' });
                },
            });
        }

        function decodeVideo(record) {
            const key = (record.flags & FLAG_KEYFRAME) !== 0;
            if (!key && waitingForKey) return;
            if (key && !configured) {
                const codec = opts.codec === 'vp8' ? 'vp8' : h264CodecString(record.payload);
                // optimizeForLatency: no frame reordering buffer. The agent never emits B-frames,
                // so there is nothing to reorder and every buffered frame is pure delay.
                decoder.configure({ codec, optimizeForLatency: true });
                configured = true;
            }
            waitingForKey = false;
            lastTimestamp = Math.max(lastTimestamp + 1, Math.round(performance.now() * 1000));
            try {
                decoder.decode(new EncodedVideoChunk({
                    type: key ? 'key' : 'delta',
                    timestamp: lastTimestamp,
                    data: record.payload,
                }));
            } catch (e) {
                newDecoder();
                send({ t: 'key' });
            }
        }

        // One long-poll. Resolves to {buffer, next, state}, to RETRY after a transient failure
        // (already slept), or to GONE when the hub says this session is not ours any more -- a
        // 404 is "gone or never yours", and not worth retrying.
        const RETRY = 'retry';
        const GONE = 'gone';
        async function fetchBatch() {
            abort = new AbortController();
            let response;
            try {
                response = await fetch(opts.downUrl(after), { signal: abort.signal });
            } catch (e) {
                if (!stopped) await sleep(RETRY_MS);
                return RETRY;
            }
            if (response.status === 404) return GONE;
            if (!response.ok) {
                await sleep(RETRY_MS);
                return RETRY;
            }
            try {
                return {
                    buffer: await response.arrayBuffer(),
                    next: Number.parseInt(response.headers.get('X-Relay-Next') || '', 10),
                    state: response.headers.get('X-Relay-State'),
                };
            } catch (e) {
                return RETRY;
            }
        }

        const statusText = new TextDecoder();
        function applyBatch(buffer) {
            if (!waitingForKey && decoder.decodeQueueSize > MAX_DECODE_QUEUE) {
                waitingForKey = true;
                send({ t: 'key' });
            }
            for (const record of parseRecords(buffer)) {
                if (record.kind === KIND_VIDEO) decodeVideo(record);
                else if (record.kind === KIND_CONTROL) opts.onControl?.(statusText.decode(record.payload));
            }
        }

        async function downLoop() {
            while (!stopped) {
                const batch = await fetchBatch();
                if (stopped) return;
                if (batch === GONE) { finish(); return; }
                if (batch === RETRY) continue;
                applyBatch(batch.buffer);
                if (Number.isFinite(batch.next)) after = batch.next;
                if (batch.state === 'closed') { finish(); return; }
            }
        }

        function finish() {
            if (stopped) return;
            stop();
            if (opts.onClosed) opts.onClosed();
        }

        // ---- Input up ----------------------------------------------------------------
        // One POST in flight at a time; whatever queued meanwhile goes in the next. Consecutive
        // mouse moves collapse to the latest -- the pointer only needs to end up in the right
        // place, and 25 positions a second queued behind a slow request would make it trail
        // the operator's hand. Nothing else is ever merged or dropped: a lost key-up is a key
        // held down on somebody's PC.
        function send(message) {
            if (stopped) return;
            if (message.t === 'm' && upQueue.at(-1)?.t === 'm') upQueue[upQueue.length - 1] = message;
            else upQueue.push(message);
            void flush();
        }

        async function flush() {
            if (upBusy || stopped || !upQueue.length) return;
            upBusy = true;
            const batch = upQueue.splice(0, 200);
            try {
                await window.FleetApi.postJson(opts.upUrl(), { messages: batch });
            } catch (e) {
                // Put it back once; the hub refusing outright (session over) ends the relay
                // through the down loop's state, not here.
                if (!stopped) {
                    upQueue.unshift(...batch);
                    await sleep(RETRY_MS);
                }
            } finally {
                upBusy = false;
            }
            void flush();
        }

        function stop() {
            if (stopped) return;
            stopped = true;
            if (abort) { try { abort.abort(); } catch (e) { /* already done */ } }
            if (decoder) { try { decoder.close(); } catch (e) { /* already closed */ } }
            decoder = null;
            stream.getTracks().forEach((track) => track.stop());
            if (opts.video.srcObject === stream) opts.video.srcObject = null;
            upQueue.length = 0;
        }

        newDecoder();
        // The loop handles its own network errors. Anything else that escapes it (a throw from
        // the decoder or the status handler) would otherwise end it silently and leave the last
        // frame on screen looking live -- so it ends the relay visibly instead.
        downLoop().catch((e) => {
            console.error('hub relay stopped:', e);
            finish();
        });
        return { send, stop };
    }

    window.RemoteRelay = { create, supported, h264CodecString };
})();
