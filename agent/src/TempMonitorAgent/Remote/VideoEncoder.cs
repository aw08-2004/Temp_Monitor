namespace TempMonitorAgent.Remote;

/// <summary>Which codec the session negotiates. H.264 is the default and the only one with a
/// hardware path; VP8 exists as a compatibility fallback for browsers or middleboxes that
/// mishandle our H.264 stream.</summary>
public enum VideoCodec
{
    H264,
    Vp8,
}

/// <summary>Operator preference for the encoder implementation. <see cref="Auto"/> tries
/// hardware and silently falls back, which is what you want in the fleet; the forced modes
/// exist for diagnosing a machine where one path misbehaves.</summary>
public enum EncoderPreference
{
    Auto,
    Hardware,
    Software,
}

/// <summary>
/// One video encoder feeding the WebRTC track.
///
/// Deliberately narrow: the pipeline hands it NV12 and gets back bytes ready for
/// <c>RemotePeer.SendFrame</c>. There is no reconfigure method — changing resolution, frame
/// rate, bitrate or codec disposes the encoder and builds a new one. That is not laziness: the
/// capture pipeline already has to rebuild on a desktop switch or a resolution change, so
/// routing every change through the one path means there is a single, well-tested way for the
/// stream to re-key, and a fresh encoder always emits SPS/PPS + IDR, which is exactly what the
/// browser's decoder needs to pick the stream back up.
/// </summary>
public interface IVideoEncoder : IDisposable
{
    /// <summary>True when a hardware MFT (NVENC/QuickSync/AMF) is driving. Surfaced to the
    /// operator so a machine quietly falling back to software is visible rather than just slow.</summary>
    bool IsHardware { get; }

    /// <summary>Short name for logs and the viewer status line, e.g. "H.264 (hardware)".</summary>
    string Description { get; }

    /// <summary>Encode one NV12 frame. Returns the bytes produced for it — usually one access
    /// unit, occasionally empty while the encoder buffers.</summary>
    byte[] Encode(byte[] nv12, int nv12Length, long timestamp100ns, long duration100ns);
}

/// <summary>Everything that determines encoder identity. Two settings that compare equal can
/// share an encoder; anything else means a rebuild.</summary>
public readonly record struct EncoderSettings(
    VideoCodec Codec,
    EncoderPreference Preference,
    int Width,
    int Height,
    int Fps,
    int BitrateBps);
