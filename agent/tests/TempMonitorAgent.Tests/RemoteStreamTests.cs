using TempMonitorAgent.Remote;

namespace TempMonitorAgent.Tests;

/// <summary>
/// The stream-shaping logic that the operator's quality controls drive, and the input queue
/// that carries their clicks. All pure, so all testable without a GPU, an encoder or a desktop.
/// </summary>
public class RemoteStreamTests
{
    private static byte[] Bgra(int width, int height, int stride, Func<int, int, (byte b, byte g, byte r)> pixel)
    {
        var buf = new byte[stride * height];
        for (int y = 0; y < height; y++)
            for (int x = 0; x < width; x++)
            {
                var (b, g, r) = pixel(x, y);
                int p = y * stride + x * 4;
                buf[p] = b; buf[p + 1] = g; buf[p + 2] = r; buf[p + 3] = 255;
            }
        return buf;
    }

    // ---------------------------------------------------------------- scaling
    [Fact]
    public void Scaled_AtNativeSize_MatchesTheUnscaledConversion()
    {
        // Resolution scaling is the operator's biggest bandwidth lever, so 100% must be exactly
        // the old path -- otherwise "Quality" would quietly differ from what shipped before.
        const int w = 8, h = 6, stride = w * 4;
        var src = Bgra(w, h, stride, (x, y) => ((byte)(x * 20), (byte)(y * 30), (byte)(x + y)));

        var direct = new byte[ColorConvert.Nv12Size(w, h)];
        ColorConvert.BgraToNv12(src, stride, w, h, direct);

        var scaled = new byte[ColorConvert.Nv12Size(w, h)];
        ColorConvert.BgraToNv12Scaled(src, stride, w, h, w, h, scaled, Array.Empty<byte>());

        Assert.Equal(direct, scaled);
    }

    [Fact]
    public void Scaled_HalvingASolidImage_KeepsTheColour()
    {
        // A box filter over a uniform source must be an identity on colour; if it is not, the
        // averaging arithmetic is wrong and every downscaled stream is subtly off-hue.
        const int w = 8, h = 8, stride = w * 4;
        var src = Bgra(w, h, stride, (_, _) => (0, 0, 255)); // pure red
        int dw = 4, dh = 4;

        var nv12 = new byte[ColorConvert.Nv12Size(dw, dh)];
        var scratch = new byte[ColorConvert.ScratchSize(dw, dh)];
        ColorConvert.BgraToNv12Scaled(src, stride, w, h, dw, dh, nv12, scratch);

        // BT.601 limited-range red: Y=82, U=90, V=240 (same values the unscaled tests pin).
        Assert.All(nv12[..(dw * dh)], y => Assert.Equal(82, y));
        Assert.Equal(90, nv12[dw * dh]);
        Assert.Equal(240, nv12[dw * dh + 1]);
    }

    [Fact]
    public void Scaled_AveragesRatherThanPointSampling()
    {
        // Half black, half white columns downscaled 2:1 must yield mid-grey. Nearest-neighbour
        // would yield pure black or pure white -- which is what makes downscaled TEXT unreadable.
        const int w = 4, h = 2, stride = w * 4;
        var src = Bgra(w, h, stride, (x, _) => x % 2 == 0 ? ((byte)0, (byte)0, (byte)0)
                                                          : ((byte)255, (byte)255, (byte)255));
        int dw = 2, dh = 2;
        var nv12 = new byte[ColorConvert.Nv12Size(dw, dh)];
        var scratch = new byte[ColorConvert.ScratchSize(dw, dh)];
        ColorConvert.BgraToNv12Scaled(src, stride, w, h, dw, dh, nv12, scratch);

        // Averaged 0 and 255 -> 127; luma of mid-grey sits well inside the 16..235 range.
        Assert.All(nv12[..(dw * dh)], y => Assert.InRange(y, 120, 140));
    }

    [Fact]
    public void Scaled_RefusesToUpscale()
    {
        var src = Bgra(4, 4, 16, (_, _) => (0, 0, 0));
        Assert.Throws<ArgumentException>(() => ColorConvert.BgraToNv12Scaled(
            src, 16, 4, 4, 8, 8, new byte[ColorConvert.Nv12Size(8, 8)],
            new byte[ColorConvert.ScratchSize(8, 8)]));
    }

    // ---------------------------------------------------------------- settings
    [Fact]
    public void Sanitized_ClampsValuesThePipelineCannotHonour()
    {
        // fps 0 would divide by zero in the frame-interval maths; the rest would produce a
        // stream nobody asked for. These arrive from a browser, so none of them are trusted.
        var settings = new StreamSettings(Monitor: 99, Fps: 0, BitrateBps: 1, ScalePercent: 0,
                                          VideoCodec.H264, EncoderPreference.Auto).Sanitized();
        Assert.Equal(15, settings.Monitor);
        Assert.Equal(1, settings.Fps);
        Assert.Equal(100_000, settings.BitrateBps);
        Assert.Equal(25, settings.ScalePercent);
    }

    [Fact]
    public void LiveStreamSettings_UpdateSanitizesAndPublishes()
    {
        var live = new LiveStreamSettings(StreamSettings.Default);
        var updated = live.Update(current => current with { Fps = 500 });
        Assert.Equal(60, updated.Fps);
        Assert.Equal(60, live.Current.Fps);
    }

    [Fact]
    public void SessionParams_UnknownCodecAndEncoderDegradeToTheGoodPath()
    {
        // An older hub, a replayed command, or a typo must not fail the session outright.
        var session = new RemoteSessionParams { Codec = "av1", Encoder = "gpu-please" };
        Assert.Equal(VideoCodec.H264, session.ParsedCodec);
        Assert.Equal(EncoderPreference.Auto, session.ParsedEncoder);
    }

    [Fact]
    public void SessionParams_RoundTripThroughJsonCarriesTheNewFields()
    {
        var original = new RemoteSessionParams
        {
            SessionId = "abc123",
            Monitor = 2,
            TargetSession = 3,
            Fps = 30,
            BitrateKbps = 8000,
            Scale = 50,
            Codec = "vp8",
            Encoder = "hardware",
        };
        var restored = RemoteSessionParams.FromJson(original.ToJson());
        Assert.NotNull(restored);
        Assert.Equal(3, restored!.TargetSession);
        Assert.Equal(VideoCodec.Vp8, restored.ParsedCodec);
        Assert.Equal(EncoderPreference.Hardware, restored.ParsedEncoder);

        var stream = restored.ToStreamSettings();
        Assert.Equal(30, stream.Fps);
        Assert.Equal(8_000_000, stream.BitrateBps);
        Assert.Equal(50, stream.ScalePercent);
        Assert.Equal(2, stream.Monitor);
    }

    [Fact]
    public void SessionParams_OlderHubWithoutTheNewFieldsGetsWorkingDefaults()
    {
        var restored = RemoteSessionParams.FromJson("""{"session_id":"x","monitor":0}""");
        Assert.NotNull(restored);
        Assert.Null(restored!.TargetSession);       // i.e. "auto"
        var stream = restored.ToStreamSettings();
        Assert.Equal(15, stream.Fps);
        Assert.Equal(100, stream.ScalePercent);
    }

    // ---------------------------------------------------------------- input queue
    [Fact]
    public void InputQueue_DeliversInOrder()
    {
        using var queue = new InputQueue();
        queue.Enqueue("""{"t":"d","b":0}""");
        queue.Enqueue("""{"t":"u","b":0}""");

        Assert.True(queue.TryTake(out var first, 100));
        Assert.True(queue.TryTake(out var second, 100));
        Assert.Contains("\"d\"", first);
        Assert.Contains("\"u\"", second);
    }

    [Fact]
    public void InputQueue_UnderPressure_DropsMouseMovesAndKeepsKeys()
    {
        // The asymmetry is the point: a dropped mouse-move is a stale approximation nobody
        // misses, but a dropped keyUp leaves a modifier stuck down on the remote machine, so
        // the operator's next keystroke arrives as Ctrl+something.
        using var queue = new InputQueue(capacity: 4);
        queue.Enqueue("""{"t":"m","x":0.1,"y":0.1}""");
        queue.Enqueue("""{"t":"k","code":"ControlLeft","down":true}""");
        queue.Enqueue("""{"t":"m","x":0.2,"y":0.2}""");
        queue.Enqueue("""{"t":"k","code":"ControlLeft","down":false}""");
        queue.Enqueue("""{"t":"d","b":0}""");   // over capacity: a move must give way

        var drained = new List<string>();
        while (queue.TryTake(out var item, 50)) drained.Add(item);

        Assert.Equal(2, drained.Count(i => i.Contains("\"k\"")));
        Assert.Contains(drained, i => i.Contains("\"d\""));
        Assert.Equal(1, queue.Dropped);
    }

    [Fact]
    public void InputQueue_CompleteWakesAWaiter()
    {
        var queue = new InputQueue();
        queue.Complete();
        // Must not block for the full timeout, and must not hand back a phantom event.
        Assert.False(queue.TryTake(out _, 1000));
    }

    // ---- H.264 keyframe detection ---------------------------------------------------------
    // This is what holds a hardware encoder to the GOP it was asked for: a real capture showed
    // one emitting a single IDR in eight seconds while accepting every keyframe-spacing setting
    // we gave it, so the bitstream is read rather than the setting trusted. Getting the scan
    // wrong in either direction is expensive -- a false negative rebuilds the encoder every two
    // seconds, a false positive lets a viewer sit on black forever.

    /// <summary>An Annex-B access unit: 4-byte start code + NAL header per unit.</summary>
    private static byte[] AccessUnit(params int[] nalTypes)
    {
        var bytes = new List<byte>();
        foreach (var type in nalTypes)
        {
            bytes.AddRange(new byte[] { 0, 0, 0, 1 });
            bytes.Add((byte)(type & 0x1f));   // forbidden_zero=0, nal_ref_idc=0
            bytes.AddRange(new byte[] { 0xAA, 0xBB, 0xCC });
        }
        return bytes.ToArray();
    }

    [Fact]
    public void ContainsIdr_FindsKeyframeAccessUnits()
    {
        // What the software encoder emits at a keyframe: AUD, SPS, PPS, SEI, IDR.
        Assert.True(H264Encoder.ContainsIdr(AccessUnit(9, 7, 8, 6, 5)));
        Assert.True(H264Encoder.ContainsIdr(AccessUnit(5)));          // bare IDR
        Assert.True(H264Encoder.ContainsIdr(AccessUnit(9, 7, 8)));    // parameter sets alone
    }

    [Fact]
    public void ContainsIdr_RejectsInterFrames()
    {
        // The hardware encoder's steady state: AUD, PPS, SEI, non-IDR slice -- note the PPS,
        // which must NOT count as a keyframe on its own or the watchdog never fires.
        Assert.False(H264Encoder.ContainsIdr(AccessUnit(9, 8, 6, 1)));
        Assert.False(H264Encoder.ContainsIdr(AccessUnit(1)));
        Assert.False(H264Encoder.ContainsIdr(Array.Empty<byte>()));
    }

    [Fact]
    public void ContainsIdr_HandlesThreeByteStartCodesAndTruncation()
    {
        // Encoders mix 3- and 4-byte start codes within one access unit.
        Assert.True(H264Encoder.ContainsIdr(new byte[] { 0, 0, 0, 1, 9, 0x10, 0, 0, 1, 5, 0x88 }));
        Assert.False(H264Encoder.ContainsIdr(new byte[] { 0, 0, 0, 1, 1, 0x88, 0, 0, 1 }));
        // A start code with no payload byte must not read past the end.
        Assert.False(H264Encoder.ContainsIdr(new byte[] { 0, 0, 0, 1 }));
    }
}
