namespace TempMonitorAgent.Remote;

/// <summary>
/// Where a session's encoded frames and status messages go (roadmap #2): the WebRTC peer
/// (<see cref="RemotePeer"/>), or the hub relay it falls back to when WebRTC cannot connect
/// (<see cref="HubRelayClient"/>).
///
/// Narrow on purpose -- the two calls the capture loop and the helper already made on the peer
/// -- so the capture pipeline does not know which transport it is feeding, and a session can be
/// moved from one to the other without stopping capture.
/// </summary>
public interface IRemoteMediaSink
{
    /// <summary>Send one encoded frame. <paramref name="durationRtpUnits"/> is the frame duration
    /// in the 90 kHz RTP clock; the relay ignores it (it has no clock to keep).</summary>
    void SendFrame(byte[] encoded, uint durationRtpUnits);

    /// <summary>Send a status message (geometry, capture stalled) to the viewer. Best-effort on
    /// every transport: it is diagnostics, and must never disturb the capture loop.</summary>
    void SendControl(string json);
}

/// <summary>
/// The sink the capture thread actually holds: forwards to whichever transport is current.
///
/// Switching is a single volatile reference swap, so the capture thread never blocks on it and
/// never sees a half-switched state -- a frame goes wholly to the old sink or wholly to the new
/// one. Frames the old sink was given just before the swap are lost with it, which is fine:
/// the switch also requests a keyframe, and the new viewer can start at nothing earlier.
/// </summary>
public sealed class MediaRouter : IRemoteMediaSink
{
    private IRemoteMediaSink _current;

    public MediaRouter(IRemoteMediaSink initial) => _current = initial;

    public IRemoteMediaSink Current => Volatile.Read(ref _current);

    public void Use(IRemoteMediaSink sink) => Volatile.Write(ref _current, sink);

    public void SendFrame(byte[] encoded, uint durationRtpUnits) =>
        Current.SendFrame(encoded, durationRtpUnits);

    public void SendControl(string json) => Current.SendControl(json);
}
