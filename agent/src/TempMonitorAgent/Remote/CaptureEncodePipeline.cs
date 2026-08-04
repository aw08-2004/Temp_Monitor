using System.Diagnostics;

namespace TempMonitorAgent.Remote;

/// <summary>
/// Ties capture -> scale -> NV12 -> video encoder together (roadmap #2) and drives it two ways:
/// a live feed to a <see cref="RemotePeer"/> (the real session), and a standalone self-test that
/// writes an Annex-B <c>.h264</c> file so the capture + encode half can be validated on a real
/// machine (run the agent binary with <c>--remote-capture-test</c>) with no hub, browser, or
/// session-injection involved. Both paths run the same loop, so what the file test proves is
/// exactly what the live session does.
///
/// The loop is built around one idea: <b>anything that changes the shape of the stream is a
/// rebuild</b>. A desktop switch, a resolution change, a quality change from the operator, a
/// duplication that died and will not come back -- all of them dispose the capture and the
/// encoder and open fresh ones. Funnelling every change through a single path means there is
/// one well-exercised way for the stream to re-key, and a fresh encoder always emits SPS/PPS +
/// IDR, which is exactly what a browser's decoder needs to pick the stream back up.
///
/// The capture thread must be attached to the current input desktop before the DXGI duplication
/// is built, or <c>DuplicateOutput</c> returns ACCESS_DENIED. That ordering -- attach, then
/// rebuild -- is why the lock screen used to permanently kill the stream and now does not.
/// </summary>
public sealed class CaptureEncodePipeline
{
    /// <summary>How long a capture may produce nothing before we declare it stalled and tell
    /// the viewer. Long enough not to fire on an idle desktop, short enough that an operator
    /// staring at a frozen frame gets an explanation.</summary>
    private const int StallThresholdMs = 5000;

    /// <summary>How long a capture that HAS a picture may go without a new one before we rebuild
    /// it anyway. This is a backstop for a wedged driver, not stall detection: a desktop nobody
    /// is touching legitimately produces no frames for hours, so this must never be short enough
    /// to churn an idle session.</summary>
    private const int IdleRebuildMs = 30_000;

    /// <summary>Ceiling on the gap we will report between two sent frames (see
    /// <see cref="RtpDurationFor"/>). An encoder rebuild or a stalled hardware MFT can leave a
    /// multi-second hole; reporting it honestly is right up to a point, but an unbounded jump in
    /// the RTP clock is worth capping, and a gap this large re-keys the stream anyway.</summary>
    private const int MaxFrameGapMs = 2000;

    /// <summary>Reported to the caller whenever the stream's shape changes, so the browser can
    /// be told what it is now looking at.</summary>
    public readonly record struct Geometry(
        int Width, int Height, string Desktop, string Encoder, int Monitor, int MonitorCount);

    /// <summary>Capture <paramref name="seconds"/> to an Annex-B <c>.h264</c> file. Returns the
    /// number of encoded frames. <paramref name="preference"/> is exposed here (unlike the live
    /// path, which takes it from the session) so the self-test can pin hardware or software on a
    /// machine where one of the two misbehaves -- which is the first thing worth knowing when
    /// H.264 produces nothing.</summary>
    public static int RunToFile(
        string outputPath, int seconds, int monitor, int fps, int bitrateBps, Action<string> log,
        EncoderPreference preference = EncoderPreference.Auto)
    {
        using var file = new FileStream(outputPath, FileMode.Create, FileAccess.Write);
        var sw = Stopwatch.StartNew();
        var settings = new LiveStreamSettings(
            StreamSettings.Default with
            {
                Monitor = monitor, Fps = fps, BitrateBps = bitrateBps, Preference = preference,
            });

        int frames = RunLoop(settings,
            keepGoing: () => sw.Elapsed.TotalSeconds < seconds,
            onEncoded: (bytes, _) => { file.Write(bytes, 0, bytes.Length); },
            log: log);
        file.Flush();
        log($"wrote {frames} frames, {file.Length} bytes");
        return frames;
    }

    /// <summary>Stream captured, encoded frames to a WebRTC peer until cancelled. Must run on a
    /// thread that owns <paramref name="binder"/> (see the class remarks).</summary>
    public static void RunToPeer(
        RemotePeer peer, LiveStreamSettings settings, CancellationToken ct, Action<string> log,
        InputDesktopWatcher? desktops = null, ThreadDesktopBinder? binder = null,
        Action<Geometry>? onGeometry = null, Action<string>? onStall = null)
    {
        RunLoop(settings,
            keepGoing: () => !ct.IsCancellationRequested,
            onEncoded: (bytes, durationRtp) => peer.SendFrame(bytes, durationRtp),
            log: log, ct: ct, desktops: desktops, binder: binder,
            onGeometry: onGeometry, onStall: onStall);
    }

    /// <summary>The shared loop. See the class remarks for why every change is a rebuild.</summary>
    private static int RunLoop(
        LiveStreamSettings live,
        Func<bool> keepGoing, Action<byte[], uint> onEncoded, Action<string> log,
        CancellationToken ct = default,
        InputDesktopWatcher? desktops = null, ThreadDesktopBinder? binder = null,
        Action<Geometry>? onGeometry = null, Action<string>? onStall = null)
    {
        CaptureSession? session = null;
        int frames = 0;
        long timestamp = 0;
        bool stallReported = false;
        var sinceLastFrame = Stopwatch.StartNew();
        var tick = Stopwatch.StartNew();
        // Wall clock since the last frame we actually SENT, which is what the RTP timestamps are
        // derived from. Separate from `tick` (which paces the loop) and from `sinceLastFrame`
        // (which is stall detection) because it must only advance across frames that reached the
        // peer -- a frame the encoder swallowed contributes its time to the next one that lands.
        var sinceSent = Stopwatch.StartNew();

        try
        {
            while (keepGoing())
            {
                // 1. Follow the input desktop FIRST. The duplication we build below is only
                //    valid for the desktop this thread is attached to.
                bool desktopSwitched = binder?.SyncTo(desktops) ?? false;

                // 2. A settings change (quality, monitor, codec) is just another rebuild reason.
                var wanted = live.Current;
                bool settingsChanged = session is not null && !session.Matches(wanted);

                if (session is null || desktopSwitched || settingsChanged)
                {
                    string reason = session is null ? "start"
                                  : desktopSwitched ? $"desktop switch -> {binder?.AttachedName ?? "?"}"
                                  : "settings change";
                    session?.Dispose();
                    session = CaptureSession.Open(wanted, log);
                    if (session is null)
                    {
                        // No frame at all yet -- normal while a desktop switch is in flight, or on
                        // a headless box with no display. Back off and retry rather than exiting:
                        // the whole point of the virtual-display work is that this can recover.
                        ReportStallOnce(ref stallReported, sinceLastFrame, onStall,
                                        binder?.AttachedName);
                        if (!SleepOrBreak(200, ct)) break;
                        continue;
                    }
                    log($"capture rebuilt ({reason}): {session.Width}x{session.Height} @ " +
                        $"{wanted.Fps}fps, {wanted.BitrateBps / 1000}kbps, {session.EncoderDescription}" +
                        (wanted.ScalePercent == 100 ? "" : $", scaled to {wanted.ScalePercent}%") +
                        $", desktop {binder?.AttachedName ?? Desktops.CurrentThreadDesktopName() ?? "?"}");
                    onGeometry?.Invoke(new Geometry(
                        session.Width, session.Height,
                        binder?.AttachedName ?? Desktops.CurrentThreadDesktopName() ?? "",
                        session.EncoderDescription, wanted.Monitor, session.MonitorCount));
                    stallReported = false;
                    sinceLastFrame.Restart();
                }

                int frameIntervalMs = Math.Max(1, 1000 / wanted.Fps);
                bool fresh = session.CaptureFrame(frameIntervalMs);

                // 3. A resolution change under us (lock screen on a virtual display, a mode
                //    change) needs a new encoder -- the old one is sized for the old frame.
                if (session.SourceSizeChanged)
                {
                    log($"capture source resized to {session.SourceWidth}x{session.SourceHeight}; rebuilding");
                    session.Dispose();
                    session = null;
                    continue;
                }

                // 4. Nothing arriving is two different situations, and treating them the same
                //    is what made the logon screen unusable. A capture that has never produced
                //    a picture is broken: rebuild it quickly and say so. A capture that HAS a
                //    picture and has merely stopped changing is an idle desktop -- the normal
                //    state of a logon screen -- so keep encoding the frame we hold, stay quiet,
                //    and only rebuild much later as a backstop against a wedged driver.
                bool blind = !session.HasFrame;
                int deadline = blind ? StallThresholdMs : IdleRebuildMs;
                if (!fresh && session.DeadFor(deadline))
                {
                    log($"capture produced nothing for {deadline}ms " +
                        $"({(blind ? "no frame ever" : "screen idle")}); rebuilding");
                    session.Dispose();
                    session = null;
                    continue;
                }

                if (fresh) { sinceLastFrame.Restart(); stallReported = false; }
                else if (blind)
                    ReportStallOnce(ref stallReported, sinceLastFrame, onStall, binder?.AttachedName);

                long frameDuration100ns = 10_000_000L / wanted.Fps;
                var encoded = session.Encode(timestamp, frameDuration100ns);
                if (encoded.Length > 0)
                {
                    onEncoded(encoded, RtpDurationFor(sinceSent.ElapsedMilliseconds, wanted.Fps));
                    sinceSent.Restart();
                    frames++;
                }
                timestamp += frameDuration100ns;

                int sleep = frameIntervalMs - (int)tick.ElapsedMilliseconds;
                if (sleep > 0 && !SleepOrBreak(sleep, ct)) break;
                tick.Restart();
            }
        }
        finally
        {
            session?.Dispose();
        }
        return frames;
    }

    /// <summary>
    /// How far to advance the 90 kHz RTP clock for the frame about to be sent, measured from the
    /// wall clock rather than assumed from the configured fps.
    ///
    /// This used to be a flat <c>90000 / fps</c>, which is correct exactly as long as the loop
    /// keeps up -- and the loop stops keeping up in precisely the conditions where latency already
    /// hurts most: a software encoder on a 4K desktop, a hardware MFT stalling in AwaitNeedInput,
    /// a rebuild. When an iteration takes 120ms at a nominal 15fps, we were telling the receiver
    /// the frames were 66ms apart while they actually arrived 120ms apart. A WebRTC receiver reads
    /// that mismatch as network jitter and GROWS its buffer to absorb it -- so the punishment for
    /// a slow encoder was not just a lower frame rate but a jitter buffer that inflated and stayed
    /// inflated, adding delay that never came back on its own. Measuring the real gap keeps the
    /// receiver's clock model honest, so the only latency left is the latency actually there.
    ///
    /// Note this is deliberately NOT applied to the Media Foundation sample times, which stay on
    /// the nominal monotonic clock: those feed the encoder's own rate control, which was
    /// configured for that frame rate on the media type, and the receiver never sees them.
    /// </summary>
    internal static uint RtpDurationFor(long elapsedMs, int fps)
    {
        // The first frame has no predecessor to measure against, and a sub-millisecond gap
        // rounds to zero -- both take the nominal duration rather than stalling the clock.
        long nominalMs = Math.Max(1, 1000L / Math.Max(1, fps));
        if (elapsedMs <= 0) elapsedMs = nominalMs;

        // 90 kHz clock: 90 ticks per millisecond.
        return (uint)(Math.Clamp(elapsedMs, 1, MaxFrameGapMs) * 90);
    }

    private static void ReportStallOnce(
        ref bool reported, Stopwatch since, Action<string>? onStall, string? desktop)
    {
        if (reported || since.ElapsedMilliseconds < StallThresholdMs) return;
        reported = true;
        onStall?.Invoke(desktop ?? "");
    }

    private static bool SleepOrBreak(int ms, CancellationToken ct)
    {
        if (ct.IsCancellationRequested) return false;
        return !ct.WaitHandle.WaitOne(ms);
    }

    /// <summary>
    /// One capture + encoder pair, sized for one set of settings on one desktop. Everything that
    /// has to be torn down together lives here so the loop above never has to remember the order.
    /// </summary>
    private sealed class CaptureSession : IDisposable
    {
        private readonly StreamSettings _settings;
        private readonly IScreenCapture _capture;
        private readonly IVideoEncoder _encoder;
        private readonly byte[] _nv12;
        private readonly byte[] _scratch;
        private readonly Stopwatch _sinceFresh = Stopwatch.StartNew();

        /// <summary>Encoded frame size (post-scaling).</summary>
        public int Width { get; }
        public int Height { get; }
        /// <summary>Captured frame size, before scaling.</summary>
        public int SourceWidth { get; }
        public int SourceHeight { get; }
        public int MonitorCount { get; }
        public string EncoderDescription => _encoder.Description;
        /// <summary>The capture has a real picture in hand (see <see cref="IScreenCapture.HasFrame"/>),
        /// so silence from it means "nothing moved", not "nothing works".</summary>
        public bool HasFrame => _capture.HasFrame;
        /// <summary>The capture started handing back a different resolution than we sized for.</summary>
        public bool SourceSizeChanged { get; private set; }

        private CaptureSession(StreamSettings settings, IScreenCapture capture, IVideoEncoder encoder,
                               int sourceWidth, int sourceHeight, int width, int height,
                               int monitorCount)
        {
            _settings = settings;
            _capture = capture;
            _encoder = encoder;
            SourceWidth = sourceWidth;
            SourceHeight = sourceHeight;
            Width = width;
            Height = height;
            MonitorCount = monitorCount;
            _nv12 = new byte[ColorConvert.Nv12Size(width, height)];
            _scratch = width == sourceWidth && height == sourceHeight
                ? Array.Empty<byte>()
                : new byte[ColorConvert.ScratchSize(width, height)];
        }

        /// <summary>Open a capture and a matching encoder. Returns null when nothing could be
        /// captured -- the caller retries rather than treating that as fatal, because a desktop
        /// switch in flight and a machine waiting for its virtual display both look like this.</summary>
        public static CaptureSession? Open(StreamSettings settings, Action<string> log)
        {
            IScreenCapture capture = OpenCapture(settings.Monitor, log);
            try
            {
                if (!WaitForFirstFrame(capture, out int srcW, out int srcH))
                {
                    capture.Dispose();
                    return null;
                }
                srcW &= ~1;  // NV12 needs even dimensions
                srcH &= ~1;

                int dstW = Even(srcW * settings.ScalePercent / 100, srcW);
                int dstH = Even(srcH * settings.ScalePercent / 100, srcH);

                IVideoEncoder encoder = settings.Codec switch
                {
                    VideoCodec.Vp8 => new Vp8Encoder(dstW, dstH, settings.Fps, settings.BitrateBps),
                    _ => new H264Encoder(dstW, dstH, settings.Fps, settings.BitrateBps,
                                         settings.Preference, log),
                };
                var session = new CaptureSession(settings, capture, encoder, srcW, srcH, dstW, dstH,
                                                 DisplayProbe.OutputCount());
                // Convert what the capture already holds. An idle desktop hands us nothing more
                // until something moves, so without this the first seconds -- possibly all of
                // them, on a logon screen -- would encode a buffer of zeros.
                session.PrimeFromHeldFrame();
                return session;
            }
            catch
            {
                capture.Dispose();
                throw;
            }
        }

        /// <summary>Round down to an even value of at least 2, never above the source.</summary>
        private static int Even(int value, int max) => Math.Clamp(value & ~1, 2, max);

        /// <summary>True when this session was built for exactly these settings.</summary>
        public bool Matches(StreamSettings settings) => _settings == settings;

        /// <summary>Grab the next frame and convert it. Returns false when nothing changed, in
        /// which case the previous NV12 is retained so the encoder keeps a steady cadence.</summary>
        public bool CaptureFrame(int timeoutMs)
        {
            bool fresh = _capture.TryCapture(timeoutMs);
            if (!fresh) return false;

            _sinceFresh.Restart();
            if (_capture.Width != SourceWidth || _capture.Height != SourceHeight)
            {
                // Don't convert -- the buffers are sized for the old geometry. Flag it and let
                // the loop rebuild; converting here would read past the end of the frame.
                SourceSizeChanged = true;
                return false;
            }

            Convert();
            return true;
        }

        /// <summary>Convert the frame the capture is already holding, without waiting for a new
        /// one. Safe to call only when the held frame matches the geometry we were built for.</summary>
        internal void PrimeFromHeldFrame()
        {
            if (!_capture.HasFrame) return;
            // The odd-pixel trim Open applies means the held frame can be one pixel wider or
            // taller than what we were sized for; anything more than that is a real mismatch.
            if ((_capture.Width & ~1) != SourceWidth || (_capture.Height & ~1) != SourceHeight)
                return;
            Convert();
        }

        private void Convert()
        {
            if (_scratch.Length == 0)
                ColorConvert.BgraToNv12(_capture.Frame, _capture.Stride, Width, Height, _nv12);
            else
                ColorConvert.BgraToNv12Scaled(_capture.Frame, _capture.Stride,
                                              SourceWidth, SourceHeight, Width, Height,
                                              _nv12, _scratch);
        }

        public bool DeadFor(int ms) => _sinceFresh.ElapsedMilliseconds >= ms;

        public byte[] Encode(long timestamp100ns, long duration100ns) =>
            _encoder.Encode(_nv12, _nv12.Length, timestamp100ns, duration100ns);

        public void Dispose()
        {
            // Encoder first: it may still hold buffers derived from the capture.
            try { _encoder.Dispose(); } catch { }
            try { _capture.Dispose(); } catch { }
        }
    }

    /// <summary>
    /// Choose the capture path. The test is "can Desktop Duplication duplicate this output",
    /// NOT "did it hand us a frame in the next second": a duplication of an unchanging screen
    /// times out and is still perfectly healthy. Falling back on that alone was catastrophic on
    /// exactly the screen this feature exists for -- the logon screen is static by nature, and
    /// GDI cannot see the secure desktop at all, so the fallback traded a working capture for a
    /// permanently black one.
    /// </summary>
    private static IScreenCapture OpenCapture(int monitor, Action<string> log)
    {
        var dxgi = new DxgiScreenCapture(monitor);
        for (int i = 0; i < 10; i++)
            if (dxgi.TryCapture(100)) { log("using DXGI Desktop Duplication"); return dxgi; }

        if (dxgi.IsDuplicating)
        {
            log("using DXGI Desktop Duplication (no screen change yet -- an idle desktop or " +
                "logon screen looks like this)");
            return dxgi;
        }

        dxgi.Dispose();
        log("DXGI unavailable; using GDI BitBlt fallback (this cannot see the secure desktop)");
        return new GdiScreenCapture();
    }

    /// <summary>
    /// Wait until the capture has a picture to size the encoder against. It counts frames the
    /// capture already had -- <see cref="OpenCapture"/> above spends up to a second asking for
    /// one, and on a static screen that is the only frame we are going to be handed until
    /// something moves. Discarding it here is what used to leave an operator staring at black
    /// with "the agent is not getting any frames" on a machine whose monitor was showing the
    /// logon screen perfectly well.
    /// </summary>
    internal static bool WaitForFirstFrame(IScreenCapture capture, out int width, out int height)
    {
        for (int i = 0; i < 50; i++) // ~5s at 100ms
        {
            if ((capture.TryCapture(100) || capture.HasFrame)
                && capture.Width > 0 && capture.Height > 0)
            {
                width = capture.Width;
                height = capture.Height;
                return true;
            }
        }
        width = height = 0;
        return false;
    }
}
