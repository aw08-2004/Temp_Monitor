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
/// packetisation of the encoded frames we hand it. We only feed it encoded frames.
///
/// The "control" DataChannel carries traffic BOTH ways: input events up from the browser, and
/// status down from the agent (what geometry we are now capturing, which desktop we are on,
/// whether capture has stalled). The downstream half is diagnostics rather than correctness --
/// but without it, "the screen went black" is unguessable from the operator's side, and the
/// answer is usually something the agent already knows.
/// </summary>
public sealed class RemotePeer : IDisposable
{
    private readonly RTCPeerConnection _pc;
    private readonly Action<string> _log;
    private readonly object _iceGate = new();
    private readonly List<RTCIceCandidateInit> _pendingRemoteIce = new();
    private bool _remoteSet;
    private RTCDataChannel? _control;
    private int _localCandidates;
    private int _remoteCandidates;

    /// <summary>Fires for each local ICE candidate; the payload is ready to POST as a signal.</summary>
    public event Action<object>? OnLocalIceCandidate;
    public event Action<RTCPeerConnectionState>? OnConnectionStateChange;
    /// <summary>Fires for each control message (input event JSON) from the browser.</summary>
    public event Action<string>? OnControlMessage;

    public RemotePeer(IEnumerable<IceServerConfig> iceServers, Action<string> log,
                      VideoCodec codec = VideoCodec.H264)
    {
        _log = log;
        var config = new RTCConfiguration { iceServers = BuildIceServers(iceServers) };
        _pc = new RTCPeerConnection(config);

        // The codec is fixed for the life of the peer because it is negotiated in the SDP:
        // changing it means a new offer/answer, which is why the viewer exposes codec as a
        // start-time choice while fps/bitrate/scale are live.
        var format = codec == VideoCodec.Vp8
            // VP8, payload type 100, 90 kHz. Mandatory-to-implement in WebRTC, so this is the
            // fallback when a browser will not play our H.264.
            ? new VideoFormat(VideoCodecsEnum.VP8, 100, 90000)
            // H.264, payload type 96, 90 kHz. The fmtp is spelled out rather than left to
            // defaults: SIPSorcery emits no profile-level-id at all, and libwebrtc reads a
            // MISSING profile-level-id as Constrained Baseline level 3.1 -- a level that cannot
            // represent 1080p, which is the least we send.
            //
            // 42e033 is Constrained Baseline (42 + constraint bits e0) at level 5.1 (0x33). The
            // level is deliberately generous rather than accurate, because it CANNOT be accurate:
            // the peer is built and the offer sent before the capture pipeline knows what geometry
            // it will get, and that geometry varies a lot -- self-tests on one machine produced
            // level 4.0 for a single 1920x1080 monitor and level 5.0 for a 3840x1080 span. Level
            // 5.1 covers anything realistic, and over-advertising is the safe direction:
            // under-advertising is the bug being fixed here. level-asymmetry-allowed lets the
            // browser answer with whatever level it prefers to decode at.
            : new VideoFormat(VideoCodecsEnum.H264, 96, 90000,
                              "packetization-mode=1;profile-level-id=42e033;level-asymmetry-allowed=1");
        _pc.addTrack(new MediaStreamTrack(format, MediaStreamStatusEnum.SendOnly));

        _pc.onicecandidate += candidate =>
        {
            if (candidate is null) return;
            // Logged rather than counted. "srflx" present but no "relay" says the STUN server
            // answered and the TURN allocation did not; no "srflx" and no "relay" from a machine
            // that is not on the operator's LAN says neither was reachable at all. Both are
            // routine and neither is guessable after the fact, and this is one line per
            // candidate on a path that produces a handful per session.
            Interlocked.Increment(ref _localCandidates);
            _log($"local ICE candidate: {Summarise(candidate.candidate)}");
            OnLocalIceCandidate?.Invoke(new
            {
                candidate = candidate.candidate,
                sdpMid = candidate.sdpMid,
                sdpMLineIndex = candidate.sdpMLineIndex,
            });
        };
        _pc.onicegatheringstatechange += state =>
        {
            if (state == RTCIceGatheringState.complete)
                _log($"ICE gathering complete: {_localCandidates} local candidate(s)");
        };
        _pc.onconnectionstatechange += state =>
        {
            _log($"peer connection state: {state}");
            if (state == RTCPeerConnectionState.failed)
                _log($"ICE never found a working path: {_localCandidates} local / " +
                     $"{_remoteCandidates} remote candidate(s) were on the table. If neither side " +
                     "produced a 'relay' candidate, the TURN server was unreachable from there.");
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
        Interlocked.Increment(ref _remoteCandidates);
        _log($"remote ICE candidate: {Summarise(candidate)}");
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

    /// <summary>Send one encoded frame (an H.264 Annex-B access unit, or a VP8 frame).
    /// <paramref name="durationRtpUnits"/> is the frame duration in the 90 kHz RTP clock
    /// (90000 / fps).</summary>
    public void SendFrame(byte[] encoded, uint durationRtpUnits)
    {
        if (encoded.Length == 0) return;
        _pc.SendVideo(durationRtpUnits, encoded);
    }

    /// <summary>Send a status message to the browser over the control channel. Best-effort by
    /// design: this is diagnostics, and a viewer that misses one is no worse off than before
    /// the channel existed, so it must never disturb the capture loop that calls it.</summary>
    public void SendControl(string json)
    {
        var channel = _control;
        if (channel is null || channel.readyState != RTCDataChannelState.open) return;
        try { channel.send(json); }
        catch (Exception e) { _log($"control send failed: {e.Message}"); }
    }

    /// <summary>Condense an SDP candidate line to the three fields that decide whether a session
    /// can connect: transport, type (host/srflx/prflx/relay) and address:port. The rest is
    /// foundation/priority/component bookkeeping that only obscures the log.
    ///
    /// An unrecognised shape is returned verbatim rather than dropped -- a candidate we cannot
    /// parse is exactly the kind of thing worth seeing in full. Browser host candidates arrive
    /// as an mDNS `<uuid>.local` name rather than an address; those are left alone too, because
    /// "the address is a .local name" is itself the answer to why a LAN-only session failed.
    /// </summary>
    private static string Summarise(string? candidate)
    {
        var text = (candidate ?? "").Trim();
        if (text.Length == 0) return "(empty)";
        // candidate:<foundation> <component> <transport> <priority> <ip> <port> typ <type> ...
        var parts = text.Split(' ', StringSplitOptions.RemoveEmptyEntries);
        if (parts.Length < 8 || !string.Equals(parts[6], "typ", StringComparison.Ordinal))
            return text;
        return $"{parts[7]} {parts[2].ToLowerInvariant()} {parts[4]}:{parts[5]}";
    }

    public RTCPeerConnectionState State => _pc.connectionState;

    public void Close()
    {
        try { _pc.close(); } catch { /* already closing */ }
    }

    public void Dispose() => Close();
}
