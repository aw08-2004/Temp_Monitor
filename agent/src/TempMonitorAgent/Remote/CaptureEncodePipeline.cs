using System.Diagnostics;

namespace TempMonitorAgent.Remote;

/// <summary>
/// Ties capture -> NV12 -> H.264 together (roadmap #2) and drives it two ways: a live feed to
/// a <see cref="RemotePeer"/> (the real session), and a standalone self-test that writes an
/// Annex-B <c>.h264</c> file so the capture + encode half can be validated on a real machine
/// (run the agent binary with <c>--remote-capture-test</c>) with no hub, browser, or
/// session-injection involved. Both paths run the same capture/encode loop, so what the file
/// test proves is exactly what the live session does.
/// </summary>
public sealed class CaptureEncodePipeline
{
    /// <summary>Capture <paramref name="seconds"/> to an Annex-B <c>.h264</c> file. Returns the
    /// number of encoded frames.</summary>
    public static int RunToFile(
        string outputPath, int seconds, int monitor, int fps, int bitrateBps, Action<string> log)
    {
        using var file = new FileStream(outputPath, FileMode.Create, FileAccess.Write);
        var sw = Stopwatch.StartNew();
        int frames = RunLoop(monitor, fps, bitrateBps,
            keepGoing: () => sw.Elapsed.TotalSeconds < seconds,
            onEncoded: (bytes, _) => { file.Write(bytes, 0, bytes.Length); },
            log: log);
        file.Flush();
        log($"wrote {frames} frames, {file.Length} bytes");
        return frames;
    }

    /// <summary>Stream captured, encoded frames to a WebRTC peer until cancelled.</summary>
    public static void RunToPeer(
        RemotePeer peer, int monitor, int fps, int bitrateBps, CancellationToken ct, Action<string> log)
    {
        RunLoop(monitor, fps, bitrateBps,
            keepGoing: () => !ct.IsCancellationRequested,
            onEncoded: (bytes, durationRtp) => peer.SendFrame(bytes, durationRtp),
            log: log, ct: ct);
    }

    /// <summary>The shared loop: open a capture, size the encoder from the first frame, then
    /// capture -> convert -> encode -> sink at the requested cadence. Re-encodes the last frame
    /// on a no-change tick so the stream keeps a steady rate.</summary>
    private static int RunLoop(
        int monitor, int fps, int bitrateBps,
        Func<bool> keepGoing, Action<byte[], uint> onEncoded, Action<string> log,
        CancellationToken ct = default)
    {
        fps = fps <= 0 ? 15 : fps;
        using var capture = OpenCapture(monitor, log);

        if (!WaitForFirstFrame(capture, out int width, out int height))
        {
            log("no frame captured; nothing to encode");
            return 0;
        }
        width &= ~1;   // NV12 needs even dimensions
        height &= ~1;
        log($"capturing {width}x{height} @ {fps}fps");

        using var encoder = new H264Encoder(width, height, fps, bitrateBps);
        log($"encoder: {(encoder.IsHardware ? "hardware" : "software")} H.264 @ {bitrateBps / 1000}kbps");

        var nv12 = new byte[ColorConvert.Nv12Size(width, height)];
        long frameDuration100ns = 10_000_000L / fps;
        uint durationRtp = (uint)(90000 / fps);
        int frameIntervalMs = 1000 / fps;

        int frames = 0;
        long ts = 0;
        var tick = Stopwatch.StartNew();
        while (keepGoing())
        {
            bool fresh = capture.TryCapture(frameIntervalMs);
            if (fresh && capture.Width >= width && capture.Height >= height)
                ColorConvert.BgraToNv12(capture.Frame, capture.Stride, width, height, nv12);
            // If not fresh, re-encode the last NV12 so the cadence stays steady.

            var encoded = encoder.Encode(nv12, nv12.Length, ts, frameDuration100ns);
            if (encoded.Length > 0)
            {
                onEncoded(encoded, durationRtp);
                frames++;
            }
            ts += frameDuration100ns;

            int sleep = frameIntervalMs - (int)tick.ElapsedMilliseconds;
            if (sleep > 0)
            {
                if (ct.IsCancellationRequested) break;
                Thread.Sleep(sleep);
            }
            tick.Restart();
        }
        return frames;
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
