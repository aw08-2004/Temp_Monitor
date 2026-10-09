using TempMonitorAgent.Remote;

namespace TempMonitorAgent.Tests;

/// <summary>
/// The agent's half of the hub-relayed fallback (roadmap #2). The failure these exist to catch
/// is silent on both ends: a framing byte out of place makes the hub answer 400 to every upload
/// while the helper logs "re-keying" forever, and a keyframe test that misses an IDR leaves the
/// viewer's decoder waiting for a keyframe that, as far as the relay knows, never comes.
/// </summary>
public class HubRelayTests
{
    [Fact]
    public void Frame_MatchesTheLayoutTheHubParses()
    {
        // [kind:1][flags:1][length:4 big-endian][payload]. tests/test_remote_relay.py pins the
        // same bytes from the hub's side, so a change to either end fails one of the two.
        var body = RelayFraming.Frame(new List<(byte, byte, byte[])>
        {
            (RelayFraming.KindVideo, RelayFraming.FlagKeyframe, new byte[] { 0xAA, 0xBB }),
            (RelayFraming.KindControl, 0, new byte[] { (byte)'{', (byte)'}' }),
        });
        Assert.Equal(new byte[]
        {
            1, 1, 0, 0, 0, 2, 0xAA, 0xBB,
            2, 0, 0, 0, 0, 2, (byte)'{', (byte)'}',
        }, body);
    }

    [Fact]
    public void GeometryMessage_StartsWithTheBytesTheHubLooksFor()
    {
        // hub/remote_relay.py's _is_geom matches on the exact prefix {"t":"geom" so that it
        // need not parse every status record on the upload path. A serializer change that
        // reordered or spaced the fields would make a resynced viewer lose its click mapping
        // silently -- so the real message, not a hand-written copy of it, is checked here.
        var json = RemoteHelper.GeometryMessage(
            new CaptureEncodePipeline.Geometry(1920, 1080, "Winlogon", "H.264 (software)", 0, 2));
        Assert.StartsWith("{\"t\":\"geom\",", json);
        Assert.Contains("\"w\":1920", json);
        Assert.Contains("\"monitors\":2", json);
    }

    [Fact]
    public void Frame_OfNothingIsEmpty() =>
        Assert.Empty(RelayFraming.Frame(new List<(byte, byte, byte[])>()));

    [Theory]
    [InlineData(new byte[] { 0, 0, 0, 1, 0x65, 0x88 }, true)]                     // IDR slice
    [InlineData(new byte[] { 0, 0, 1, 0x65, 0x88 }, true)]                        // 3-byte start code
    [InlineData(new byte[] { 0, 0, 0, 1, 0x67, 0x42, 0, 0, 0, 1, 0x68, 0xCE }, true)] // SPS + PPS
    [InlineData(new byte[] { 0, 0, 0, 1, 0x09, 0xF0, 0, 0, 0, 1, 0x65, 0x88 }, true)] // AUD, then IDR
    [InlineData(new byte[] { 0, 0, 0, 1, 0x41, 0x9A }, false)]                    // non-IDR slice
    [InlineData(new byte[] { 0, 0, 0, 1, 0x06, 0x05, 0, 0, 0, 1, 0x41, 0x9A }, false)] // SEI + P slice
    [InlineData(new byte[0], false)]
    public void IsKeyframe_H264_FindsAnIdrAnywhereInTheAccessUnit(byte[] frame, bool expected) =>
        Assert.Equal(expected, RelayFraming.IsKeyframe(VideoCodec.H264, frame));

    [Theory]
    [InlineData(new byte[] { 0x10, 0x02, 0x00 }, true)]    // frame tag bit 0 clear: keyframe
    [InlineData(new byte[] { 0x11, 0x02, 0x00 }, false)]   // bit 0 set: interframe
    public void IsKeyframe_Vp8_ReadsTheFrameTag(byte[] frame, bool expected) =>
        Assert.Equal(expected, RelayFraming.IsKeyframe(VideoCodec.Vp8, frame));

    [Fact]
    public void KeyframeRequest_IsTakenExactlyOnce()
    {
        // The capture loop polls this every frame; a request that stuck would rebuild the
        // capture 25 times a second, and one that was lost would leave a relayed viewer black
        // until the encoder's next scheduled IDR.
        var live = new LiveStreamSettings(StreamSettings.Default);
        Assert.False(live.TakeKeyframeRequest());
        live.RequestKeyframe();
        live.RequestKeyframe();
        Assert.True(live.TakeKeyframeRequest());
        Assert.False(live.TakeKeyframeRequest());
    }

    [Fact]
    public void MediaRouter_SendsToWhicheverSinkIsCurrent()
    {
        var first = new RecordingSink();
        var second = new RecordingSink();
        var router = new MediaRouter(first);
        router.SendFrame(new byte[] { 1 }, 3000);
        router.SendControl("{\"t\":\"geom\"}");
        router.Use(second);
        router.SendFrame(new byte[] { 2 }, 3000);

        Assert.Single(first.Frames);
        Assert.Single(first.Controls);
        Assert.Equal(new byte[] { 2 }, Assert.Single(second.Frames));
        Assert.Empty(second.Controls);
    }

    private sealed class RecordingSink : IRemoteMediaSink
    {
        public List<byte[]> Frames { get; } = new();
        public List<string> Controls { get; } = new();
        public void SendFrame(byte[] encoded, uint durationRtpUnits) => Frames.Add(encoded);
        public void SendControl(string json) => Controls.Add(json);
    }
}
