using System.Text;
using SIPSorcery.Net;
using SIPSorceryMedia.Abstractions;

namespace TempMonitorAgent.Remote;

/// <summary>
/// The agent helper's WebRTC peer (roadmap #2): an H.264 send-only video track fed by the
/// capture/encode pipeline, plus offer/answer + trickle-ICE plumbing. The agent is the
/// offerer -- it has the media to send -- so it creates the offer, the console answers, and
/// candidates trickle both ways through the hub's signaling relay.
///
/// SIPSorcery does DTLS-SRTP, ICE (including TURN from the supplied ICE servers), and RTP
/// packetisation of the H.264 access units we hand it. We only feed it encoded frames.
///
/// A control DataChannel for input (mouse/keyboard) is added in phase 5.
/// </summary>
public sealed class RemotePeer : IDisposable
{
    private readonly RTCPeerConnection _pc;
    private readonly Action<string> _log;
    private readonly object _iceGate = new();
    private readonly List<RTCIceCandidateInit> _pendingRemoteIce = new();
    private bool _remoteSet;
    private RTCDataChannel? _control;

    /// <summary>Fires for each local ICE candidate; the payload is ready to POST as a signal.</summary>
    public event Action<object>? OnLocalIceCandidate;
    public event Action<RTCPeerConnectionState>? OnConnectionStateChange;
    /// <summary>Fires for each control message (input event JSON) from the browser.</summary>
    public event Action<string>? OnControlMessage;

    public RemotePeer(IEnumerable<IceServerConfig> iceServers, Action<string> log)
    {
        _log = log;
        var config = new RTCConfiguration { iceServers = BuildIceServers(iceServers) };
        _pc = new RTCPeerConnection(config);

        // H.264, payload type 96, 90 kHz, packetization-mode 1 -- the standard WebRTC H.264
        // profile the browser negotiates against.
        var track = new MediaStreamTrack(
            new VideoFormat(VideoCodecsEnum.H264, 96, 90000, "packetization-mode=1"),
            MediaStreamStatusEnum.SendOnly);
        _pc.addTrack(track);

        _pc.onicecandidate += candidate =>
        {
            if (candidate is null) return;
            OnLocalIceCandidate?.Invoke(new
            {
                candidate = candidate.candidate,
                sdpMid = candidate.sdpMid,
                sdpMLineIndex = candidate.sdpMLineIndex,
            });
        };
        _pc.onconnectionstatechange += state =>
        {
            _log($"peer connection state: {state}");
            OnConnectionStateChange?.Invoke(state);
        };
    }

    private static List<RTCIceServer> BuildIceServers(IEnumerable<IceServerConfig> configs)
    {
        var servers = new List<RTCIceServer>();
        foreach (var c in configs ?? Enumerable.Empty<IceServerConfig>())
        {
            foreach (var url in c.Urls ?? new List<string>())
            {
                if (string.IsNullOrWhiteSpace(url)) continue;
                var server = new RTCIceServer { urls = url };
                if (!string.IsNullOrEmpty(c.Username))
                {
                    server.username = c.Username;
                    server.credential = c.Credential;
                }
                servers.Add(server);
            }
        }
        return servers;
    }

    /// <summary>Create the "control" DataChannel the browser sends input over. Must run before
    /// <see cref="CreateOfferAsync"/> so the channel is negotiated in the offer. The agent
    /// creates it (it is the offerer); the browser picks it up via ondatachannel.</summary>
    public async Task EnableControlChannelAsync()
    {
        _control = await _pc.createDataChannel("control", null);
        _control.onmessage += (RTCDataChannel _, DataChannelPayloadProtocols _, byte[] data) =>
        {
            if (data is { Length: > 0 })
                OnControlMessage?.Invoke(Encoding.UTF8.GetString(data));
        };
    }

    /// <summary>Create the offer, set it as the local description, and return it as a
    /// signaling payload ({type, sdp}) to POST to the console.</summary>
    public async Task<object> CreateOfferAsync()
    {
        var offer = _pc.createOffer(null);
        await _pc.setLocalDescription(offer);
        return new { type = "offer", sdp = offer.sdp };
    }

    /// <summary>Apply the console's answer SDP. Any ICE candidates that arrived before the
    /// answer are flushed now -- SIPSorcery rejects candidates before the remote description
    /// exists, so they are queued rather than dropped.</summary>
    public bool ApplyAnswer(string sdp)
    {
        var result = _pc.setRemoteDescription(new RTCSessionDescriptionInit
        {
            type = RTCSdpType.answer,
            sdp = sdp,
        });
        if (result != SetDescriptionResultEnum.OK)
        {
            _log($"setRemoteDescription(answer) failed: {result}");
            return false;
        }
        lock (_iceGate)
        {
            _remoteSet = true;
            foreach (var ice in _pendingRemoteIce) _pc.addIceCandidate(ice);
            _pendingRemoteIce.Clear();
        }
        return true;
    }

    /// <summary>Add a remote ICE candidate from the console, buffering it until the answer is
    /// applied.</summary>
    public void AddRemoteIce(string? candidate, string? sdpMid, ushort sdpMLineIndex)
    {
        if (string.IsNullOrEmpty(candidate)) return;
        var init = new RTCIceCandidateInit
        {
            candidate = candidate,
            sdpMid = sdpMid,
            sdpMLineIndex = sdpMLineIndex,
        };
        lock (_iceGate)
        {
            if (!_remoteSet) { _pendingRemoteIce.Add(init); return; }
        }
        _pc.addIceCandidate(init);
    }

    /// <summary>Send one encoded H.264 access unit. <paramref name="durationRtpUnits"/> is the
    /// frame duration in the 90 kHz RTP clock (90000 / fps).</summary>
    public void SendFrame(byte[] annexB, uint durationRtpUnits)
    {
        if (annexB.Length == 0) return;
        _pc.SendVideo(durationRtpUnits, annexB);
    }

    public RTCPeerConnectionState State => _pc.connectionState;

    public void Close()
    {
        try { _pc.close(); } catch { /* already closing */ }
    }

    public void Dispose() => Close();
}
