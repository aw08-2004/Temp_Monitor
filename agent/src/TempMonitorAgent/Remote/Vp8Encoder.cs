using SIPSorceryMedia.Encoders.Codecs;
using vpxmd;

namespace TempMonitorAgent.Remote;

/// <summary>
/// VP8 encoder over libvpx, offered as a compatibility fallback to the default H.264 path.
///
/// Why it exists: our H.264 comes from the in-box Media Foundation encoder at constrained
/// baseline, which every mainstream browser decodes -- but H.264 is the one WebRTC video codec
/// that is not universally guaranteed, and a machine whose stream is unwatchable is very hard to
/// diagnose remotely. VP8 is mandatory-to-implement in WebRTC, so switching to it separates
/// "the capture pipeline is broken" from "this browser dislikes our H.264".
///
/// It is <b>not</b> the default and should not be: libvpx here is software-only, so it costs
/// meaningfully more CPU than H.264 on any machine with a hardware encoder, and on a busy server
/// that CPU comes out of whatever the machine is actually for.
///
/// libvpx consumes our NV12 buffer directly (VPX_IMG_FMT_NV12), so no extra plane shuffling is
/// needed between the capture pipeline and here.
///
/// Not thread-safe: one encoder per capture loop, same contract as <see cref="H264Encoder"/>.
/// </summary>
public sealed class Vp8Encoder : IVideoEncoder
{
    private readonly Vp8Codec _codec = new();
    private readonly int _width, _height;
    private bool _disposed;
    /// <summary>libvpx will happily open with an inter-frame; forcing the first frame to be a
    /// keyframe means a viewer that attaches immediately has something decodable.</summary>
    private bool _needKeyFrame = true;

    /// <summary>Always false: there is no hardware VP8 path here, and reporting otherwise would
    /// hide the CPU cost from the operator choosing the codec.</summary>
    public bool IsHardware => false;

    public string Description => "VP8 (software)";

    public Vp8Encoder(int width, int height, int fps, int bitrateBps)
    {
        _width = width;
        _height = height;
        // libvpx takes a target in kbps; fps is carried by the RTP timestamps rather than the
        // codec config, so it is not passed on.
        _ = fps;
        _codec.InitialiseEncoder((uint)width, (uint)height,
                                 (uint)Math.Max(50, bitrateBps / 1000));
    }

    public byte[] Encode(byte[] nv12, int nv12Length, long timestamp100ns, long duration100ns)
    {
        if (_disposed) return Array.Empty<byte>();
        _ = timestamp100ns;
        _ = duration100ns;
        try
        {
            bool forceKey = _needKeyFrame;
            _needKeyFrame = false;
            // The buffer is reused between frames and may be longer than this frame needs;
            // libvpx reads by geometry, so hand it exactly the bytes for one NV12 image.
            int expected = ColorConvert.Nv12Size(_width, _height);
            byte[] frame = nv12.Length == expected
                ? nv12
                : nv12[..Math.Min(expected, nv12Length)];
            return _codec.Encode(frame, VpxImgFmt.VPX_IMG_FMT_NV12, forceKey)
                   ?? Array.Empty<byte>();
        }
        catch
        {
            // Match H264Encoder: a failed frame is a dropped frame, not a dead session. The
            // next keyframe re-syncs the decoder.
            _needKeyFrame = true;
            return Array.Empty<byte>();
        }
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        try { _codec.Dispose(); } catch { }
    }
}
