namespace TempMonitorAgent.Remote;

/// <summary>
/// Everything the operator can tune about a live stream.
///
/// Immutable so the capture loop can compare the settings it built against the settings it is
/// asked for with a single equality check, and so a change arriving mid-frame can never be
/// half-applied.
/// </summary>
public sealed record StreamSettings(
    int Monitor,
    int Fps,
    int BitrateBps,
    /// <summary>Percentage of the captured resolution to encode (100 = native). The largest
    /// bandwidth lever available, and the only one that helps on a 4K desktop.</summary>
    int ScalePercent,
    VideoCodec Codec,
    EncoderPreference Preference)
{
    public static StreamSettings Default { get; } = new(
        Monitor: 0, Fps: 15, BitrateBps: 4_000_000, ScalePercent: 100,
        Codec: VideoCodec.H264, Preference: EncoderPreference.Auto);

    /// <summary>Clamp to values the pipeline can actually honour. Everything here arrives from
    /// an operator's browser, so it is validated rather than trusted -- an fps of 0 would divide
    /// by zero and a bitrate of 2 would produce an unwatchable stream nobody asked for.</summary>
    public StreamSettings Sanitized() => this with
    {
        Monitor = Math.Clamp(Monitor, 0, 15),
        Fps = Math.Clamp(Fps, 1, 60),
        BitrateBps = Math.Clamp(BitrateBps, 100_000, 50_000_000),
        ScalePercent = Math.Clamp(ScalePercent, 25, 100),
    };
}

/// <summary>
/// The current <see cref="StreamSettings"/>, swappable from another thread.
///
/// The viewer changes quality mid-session over the WebRTC control channel, which arrives on a
/// SIPSorcery thread; the capture loop reads it once a frame on its own thread. A single
/// volatile reference swap is all the synchronisation that needs -- the settings object itself
/// is immutable, so a reader either sees the whole old value or the whole new one.
/// </summary>
public sealed class LiveStreamSettings
{
    private StreamSettings _current;

    public LiveStreamSettings(StreamSettings initial) => _current = initial.Sanitized();

    public StreamSettings Current => Volatile.Read(ref _current);

    /// <summary>Apply a change. Returns the new value, already sanitized.</summary>
    public StreamSettings Update(Func<StreamSettings, StreamSettings> mutate)
    {
        var updated = mutate(Current).Sanitized();
        Volatile.Write(ref _current, updated);
        return updated;
    }

    private int _keyframeRequested;

    /// <summary>Ask the capture loop for a fresh keyframe as soon as possible.
    ///
    /// WebRTC never needed this: the encoder's two-second IDR interval covers a viewer whose
    /// media starts mid-GOP. The hub relay does -- switching onto it, a viewer resynchronised
    /// after falling behind, and a browser decoder that errored all leave a viewer that can show
    /// nothing until the next keyframe, and two seconds of black is exactly what an operator
    /// who just watched WebRTC fail reads as "the fallback is broken too". The capture loop
    /// honours it with a rebuild, the one path that is guaranteed to re-key (see
    /// <see cref="CaptureEncodePipeline"/>).</summary>
    public void RequestKeyframe() => Volatile.Write(ref _keyframeRequested, 1);

    /// <summary>True once per <see cref="RequestKeyframe"/> -- the capture loop's half.</summary>
    public bool TakeKeyframeRequest() => Interlocked.Exchange(ref _keyframeRequested, 0) == 1;
}
