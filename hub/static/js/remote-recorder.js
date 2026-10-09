// Session recording (roadmap #19): records what ONE viewer is showing and streams it to the hub.
//
// The browser records, not the agent: the viewer already holds the decoded picture -- the
// WebRTC MediaStream, or the canvas the hub relay draws into -- so a MediaRecorder on that
// stream costs the PC nothing. See hub/recordings.py for the whole design; the parts that
// shape this file are:
//
//   * NOTHING IS RECORDED UNTIL THE PC CONFIRMS ITS BADGE. Start asks the hub, the hub asks
//     the PC to show "Recording Screen", and only once the hub reports `recording` does the
//     MediaRecorder start. An agent too old to show a badge never confirms, and the attempt
//     fails here after BADGE_WAIT_MS with a sentence saying so.
//   * TEN MINUTES AT A TIME. The deadline is the hub's; this file runs the 60-second
//     countdown against the hub's clock (server_time) and offers Extend. The countdown is
//     drawn in the console, never on the PC, so it is never in the video.
//   * CHUNKS GO UP IN ORDER, ONE AT A TIME, numbered. A webm file is one stream, so the hub
//     refuses a gap, and a retried chunk that already landed is accepted without being stored
//     twice. A 409 is the hub saying the recording ended without us (the session ended, the
//     badge went, the deadline passed) and carries the reason.
//   * ONE STREAM PER RECORDING. If the viewer's stream is replaced -- WebRTC giving way to the
//     hub relay -- the recording ends with `stream_changed` rather than splicing a second webm
//     header into the file, which would leave everything after it unplayable.
//
// A factory, like remote.js: window.RemoteRecorder.create(...) per viewer, no globals.
(function () {
    'use strict';

    const TIMESLICE_MS = 2000;
    const VIDEO_BITS_PER_SECOND = 2500000;
    const BADGE_WAIT_MS = 25000;
    const BADGE_POLL_MS = 1000;
    const STATUS_POLL_MS = 5000;
    const TICK_MS = 1000;
    const COUNTDOWN_SECONDS = 60;
    const MAX_CHUNK_RETRIES = 3;
    const MIME_CANDIDATES = ['video/webm;codecs=vp9', 'video/webm;codecs=vp8', 'video/webm'];

    // Why a recording ended, one LITERAL key per code: the i18n test only sees literal t()
    // calls, and tests/test_recordings_web.py checks this list against recordings.END_REASONS
    // so a reason added on the hub cannot reach the console untranslated.
    const END_REASON_TEXT = {
        stopped: () => t('recordings.end_reason.stopped'),
        time_limit: () => t('recordings.end_reason.time_limit'),
        session_ended: () => t('recordings.end_reason.session_ended'),
        helper_restarted: () => t('recordings.end_reason.helper_restarted'),
        badge_failed: () => t('recordings.end_reason.badge_failed'),
        badge_timeout: () => t('recordings.end_reason.badge_timeout'),
        stream_changed: () => t('recordings.end_reason.stream_changed'),
        size_limit: () => t('recordings.end_reason.size_limit'),
    };

    function endReasonText(code) {
        return (END_REASON_TEXT[code] || END_REASON_TEXT.stopped)();
    }

    function pickMime() {
        if (typeof MediaRecorder !== 'function') return null;
        for (const mime of MIME_CANDIDATES) {
            try { if (MediaRecorder.isTypeSupported(mime)) return mime; } catch (e) { /* next */ }
        }
        return null;
    }

    function supported() { return pickMime() !== null; }

    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

    /** A fetch that keeps the status: a 409 here is information, not a failure to report. */
    async function call(method, url, body, raw) {
        const init = { method };
        if (raw !== undefined) {
            init.headers = { 'Content-Type': 'application/octet-stream' };
            init.body = raw;
        } else if (body !== undefined) {
            init.headers = { 'Content-Type': 'application/json' };
            init.body = JSON.stringify(body);
        }
        const response = await fetch(url, init);
        const data = await response.json().catch(() => ({}));
        return { status: response.status, ok: response.ok, data };
    }

    /** opts.machine     the PC
     *  opts.sessionId   () => the viewer's remote session id
     *  opts.stream      () => the MediaStream the viewer is showing right now
     *  opts.onState     (state, detail) -- 'starting' | 'recording' | 'stopping' | 'ended' |
     *                   'failed'; detail carries {seconds} while recording, {reason} at the end,
     *                   {error} on failure
     *  opts.onCountdown (secondsLeft | null) -- a number only inside the last minute */
    function create(opts) {
        const base = '/api/remote/recordings/';
        let rec = null;
        let recorder = null;
        let recordedStream = null;
        let seq = 0;
        let queue = Promise.resolve();
        let clockOffset = 0;       // server seconds minus local seconds
        let tickTimer = null;
        let lastStatusPoll = 0;
        let state = 'idle';
        let finishing = null;

        function setState(next, detail) {
            state = next;
            if (opts.onState) opts.onState(next, detail || {});
        }

        function serverNow() { return Date.now() / 1000 + clockOffset; }

        function adopt(data) {
            if (!data?.id) return;
            rec = data;
            if (data.server_time) clockOffset = data.server_time - Date.now() / 1000;
        }

        async function start(reason) {
            if (state !== 'idle' && state !== 'ended' && state !== 'failed') return;
            const mime = pickMime();
            if (!mime) {
                setState('failed', { error: t('recordings.unsupported') });
                return;
            }
            seq = 0;
            queue = Promise.resolve();
            finishing = null;
            setState('starting');
            const res = await call('POST',
                `/api/remote/${encodeURIComponent(opts.machine)}/recordings`,
                { session_id: opts.sessionId(), reason, mime });
            if (!res.ok) {
                setState('failed', { error: res.data.error || t('common.hub_error',
                                                               { status: res.status }) });
                return;
            }
            adopt(res.data);
            // Wait for the PC to confirm its badge. Not a moment longer than needed: every
            // second here is a second the operator is looking at "waiting for the PC".
            const giveUp = Date.now() + BADGE_WAIT_MS;
            while (Date.now() < giveUp) {
                await sleep(BADGE_POLL_MS);
                if (state !== 'starting') return;          // stopped meanwhile
                const poll = await call('GET', base + encodeURIComponent(rec.id));
                if (poll.ok) adopt(poll.data);
                if (rec.status === 'recording') break;
                if (rec.status === 'failed' || rec.status === 'ended') {
                    setState('failed', { error: endReasonText(rec.end_reason) });
                    return;
                }
            }
            if (rec.status !== 'recording') {
                await call('POST', base + encodeURIComponent(rec.id) + '/stop', {});
                setState('failed', { error: endReasonText('badge_timeout') });
                return;
            }
            beginRecording(mime);
        }

        function beginRecording(mime) {
            recordedStream = opts.stream();
            if (!recordedStream) {
                finish('stream_changed');
                return;
            }
            try {
                recorder = new MediaRecorder(recordedStream,
                    { mimeType: mime, videoBitsPerSecond: VIDEO_BITS_PER_SECOND });
            } catch (e) {
                finish('stopped');
                setState('failed', { error: t('recordings.unsupported') });
                return;
            }
            recorder.ondataavailable = (e) => {
                if (e.data?.size) enqueue(e.data);
            };
            recorder.start(TIMESLICE_MS);
            setState('recording', { seconds: 0 });
            tickTimer = setInterval(tick, TICK_MS);
        }

        function enqueue(blob) {
            const n = seq++;
            queue = queue.then(() => upload(n, blob));
        }

        /** What one chunk upload's answer means: true when it is settled one way or the
         *  other, false when it is worth another try (the network, or a 5xx). */
        function settled(res) {
            if (!res) return false;                         // network: retry
            if (res.ok) {
                if (res.data.deadline) rec.deadline = res.data.deadline;
                if (res.data.server_time) clockOffset = res.data.server_time - Date.now() / 1000;
                return true;
            }
            if (res.status === 409) {
                // Ended at the hub. Say why, and stop recording into nothing.
                adopt(res.data.recording);
                ended(rec.end_reason);
                return true;
            }
            if (res.status >= 500 || res.status === 0) return false;
            stopLocal();
            setState('failed', { error: res.data.error
                || t('common.hub_error', { status: res.status }) });
            return true;
        }

        async function upload(n, blob) {
            if (!rec || state === 'ended' || state === 'failed') return;
            const url = `${base}${encodeURIComponent(rec.id)}/chunks/${n}`;
            // In order and one at a time ON PURPOSE -- the hub appends to one stream -- so the
            // await inside this loop is the design, not an oversight.
            for (let attempt = 0; attempt <= MAX_CHUNK_RETRIES; attempt++) {
                const res = await call('PUT', url, undefined, blob).catch(() => null);
                if (settled(res)) return;
                await sleep(1000 * (attempt + 1));
            }
            stopLocal();
            setState('failed', { error: t('recordings.upload_failed') });
        }

        async function tick() {
            if (state !== 'recording' || !rec) return;
            // The viewer swapped its stream (WebRTC -> hub relay): this recording is over.
            if (opts.stream() !== recordedStream) {
                finish('stream_changed');
                return;
            }
            const left = Math.round(rec.deadline - serverNow());
            if (opts.onCountdown) opts.onCountdown(left <= COUNTDOWN_SECONDS ? Math.max(0, left) : null);
            if (left <= 0) {
                finish('time_limit');
                return;
            }
            setState('recording', {
                seconds: Math.max(0, Math.round(serverNow() - (rec.confirmed_at || serverNow()))),
            });
            // An ending the hub decided (the session ended, the badge could not follow the
            // lock screen) reaches us on the next chunk anyway; this finds it sooner.
            if (Date.now() - lastStatusPoll > STATUS_POLL_MS) {
                lastStatusPoll = Date.now();
                const poll = await call('GET', base + encodeURIComponent(rec.id)).catch(() => null);
                if (poll?.ok) {
                    adopt(poll.data);
                    if (rec.status !== 'recording' && state === 'recording') {
                        stopLocal();
                        ended(rec.end_reason);
                    }
                }
            }
        }

        function stopLocal() {
            if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
            if (opts.onCountdown) opts.onCountdown(null);
            if (recorder && recorder.state !== 'inactive') {
                try { recorder.stop(); } catch (e) { /* already stopped */ }
            }
            recorder = null;
            recordedStream = null;
        }

        function ended(reason) {
            stopLocal();
            setState('ended', { reason: reason || 'stopped' });
        }

        /** Stop recording: flush the last chunk, then tell the hub. Resolves once the hub has
         *  the whole file. Safe to call more than once; later calls wait on the first. */
        function finish(reason) {
            if (finishing) return finishing;
            if (!rec || (state !== 'recording' && state !== 'starting')) {
                return Promise.resolve();
            }
            const wasStarting = state === 'starting';
            setState('stopping');
            finishing = (async () => {
                if (!wasStarting && recorder && recorder.state !== 'inactive') {
                    // The final dataavailable fires before 'stop', so the last chunk is
                    // queued by the time the promise below resolves.
                    await new Promise((resolve) => {
                        recorder.addEventListener('stop', resolve, { once: true });
                        try { recorder.stop(); } catch (e) { resolve(); }
                    });
                }
                stopLocal();
                await queue.catch(() => {});
                const res = await call('POST', base + encodeURIComponent(rec.id) + '/stop',
                                       { reason: reason || 'stopped' }).catch(() => null);
                if (res?.ok) adopt(res.data);
                setState(wasStarting ? 'failed' : 'ended',
                         wasStarting ? { error: t('recordings.cancelled') }
                                     : { reason: rec?.end_reason || reason });
            })();
            return finishing;
        }

        async function extend() {
            if (state !== 'recording' || !rec) return;
            const res = await call('POST', base + encodeURIComponent(rec.id) + '/extend', {});
            if (res.ok) {
                adopt(res.data);
                if (opts.onCountdown) opts.onCountdown(null);
            } else if (res.status === 409 && res.data.recording) {
                adopt(res.data.recording);
                if (rec.status !== 'recording') ended(rec.end_reason);
            }
        }

        return {
            start,
            stop: (reason) => finish(reason || 'stopped'),
            extend,
            isActive: () => state === 'starting' || state === 'recording' || state === 'stopping',
        };
    }

    window.RemoteRecorder = { create, supported, endReasonText };
})();
