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

    /// <summary>Reported to the caller whenever the stream's shape changes, so the browser can
    /// be told what it is now looking at.</summary>
    public readonly record struct Geometry(
        int Width, int Height, string Desktop, string Encoder, int Monitor, int MonitorCount);

    /// <summary>Capture <paramref name="seconds"/> to an Annex-B <c>.h264</c> file. Returns the
    /// number of encoded frames.</summary>
    public static int RunToFile(
        string outputPath, int seconds, int monitor, int fps, int bitrateBps, Action<string> log)
    {
        using var file = new FileStream(outputPath, FileMode.Create, FileAccess.Write);
        var sw = Stopwatch.StartNew();
        var settings = new LiveStreamSettings(
            StreamSettings.Default with { Monitor = monitor, Fps = fps, BitrateBps = bitrateBps });

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

                // 4. Duplication that died and has stayed dead is not going to fix itself from
                //    inside; rebuild it (which also re-runs the DXGI-then-GDI fallback).
                if (!fresh && session.DeadFor(StallThresholdMs))
                {
                    log("capture produced nothing for " + StallThresholdMs + "ms; rebuilding");
                    session.Dispose();
                    session = null;
                    continue;
                }

                if (fresh) { sinceLastFrame.Restart(); stallReported = false; }
                else ReportStallOnce(ref stallReported, sinceLastFrame, onStall, binder?.AttachedName);

                long frameDuration100ns = 10_000_000L / wanted.Fps;
                var encoded = session.Encode(timestamp, frameDuration100ns);
                if (encoded.Length > 0)
                {
                    onEncoded(encoded, (uint)(90000 / wanted.Fps));
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
                                         settings.Preference),
                };
                return new CaptureSession(settings, capture, encoder, srcW, srcH, dstW, dstH,
                                          DisplayProbe.OutputCount());
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

            if (_scratch.Length == 0)
                ColorConvert.BgraToNv12(_capture.Frame, _capture.Stride, Width, Height, _nv12);
            else
                ColorConvert.BgraToNv12Scaled(_capture.Frame, _capture.Stride,
                                              SourceWidth, SourceHeight, Width, Height,
                                              _nv12, _scratch);
            return true;
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

    private static IScreenCapture OpenCapture(int monitor, Action<string> log)
    {
        var dxgi = new DxgiScreenCapture(monitor);
        for (int i = 0; i < 10; i++)
            if (dxgi.TryCapture(100)) { log("using DXGI Desktop Duplication"); return dxgi; }
        dxgi.Dispose();
        log("DXGI unavailable; using GDI BitBlt fallback");
        return new GdiScreenCapture();
    }

    private static bool WaitForFirstFrame(IScreenCapture capture, out int width, out int height)
    {
        for (int i = 0; i < 50; i++) // ~5s at 100ms
        {
            if (capture.TryCapture(100) && capture.Width > 0 && capture.Height > 0)
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
