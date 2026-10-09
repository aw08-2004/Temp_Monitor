using System.Net;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Text;
using System.Text.Json.Serialization;

namespace TempMonitorAgent.Remote;

/// <summary>
/// The agent's half of the hub-relayed fallback (roadmap #2, hub/remote_relay.py): when WebRTC
/// cannot find a path, the session's already-encoded frames are POSTed to the hub and the
/// console's input is long-polled back from it, over the same HTTPS the agent reports on.
///
/// <b>Why the agent never decides to relay on its own.</b> The console asks (a <c>relay</c>
/// signal), because the hub only opens a relay for a console that asked -- an agent pushing
/// video into a session nobody switched over is refused -- and because only the browser knows
/// whether it can decode the stream itself.
///
/// <b>Uploads batch themselves.</b> One POST is in flight at a time and everything encoded
/// meanwhile goes in the next one, so a slow uplink produces bigger requests rather than more
/// of them. When even that falls behind, queued video is dropped wholesale and a keyframe is
/// requested: on a remote-control screen, seconds-old frames delivered faithfully are worse than
/// a short jump to the present.
///
/// <b>Nothing here touches a desktop.</b> It is plain HTTP on thread-pool tasks, which is why it
/// may use the pool when the capture and input threads may not (see <see cref="RemoteHelper"/>).
/// Input it receives is handed to the same <see cref="InputQueue"/> the WebRTC control channel
/// feeds, so it is applied on the desktop-bound input thread like any other.
/// </summary>
public sealed class HubRelayClient : IRemoteMediaSink, IDisposable
{
    /// <summary>Video frames allowed to wait for upload before the backlog is dropped. About
    /// three seconds at the default 15 fps.</summary>
    private const int MaxQueuedFrames = 45;

    /// <summary>Status messages allowed to wait. They are tiny and rare; this only bounds a
    /// hub that has stopped answering.</summary>
    private const int MaxQueuedControl = 64;

    private const int RetryDelayMs = 1000;

    private readonly HttpClient _http;
    private readonly string _downUrl;
    private readonly string _upUrl;
    private readonly VideoCodec _codec;
    private readonly LiveStreamSettings _settings;
    private readonly Action<string> _log;
    private readonly Action<string> _onInput;
    private readonly CancellationTokenSource _cts = new();
    private readonly SemaphoreSlim _pending = new(0);
    private readonly object _gate = new();
    private readonly List<(byte Kind, byte Flags, byte[] Payload)> _queue = new();
    private int _queuedFrames;
    private int _queuedControl;
    private bool _waitingForKey = true;
    private int _ended;
    private long _bytesSent;
    private int _framesSent;
    private int _framesDropped;

    /// <summary>Raised once, with a reason, when the hub says this relay is gone (the session
    /// ended, or was never switched over). The helper ends the session on it.</summary>
    public event Action<string>? Ended;

    public HubRelayClient(string sessionId, string bearer, VideoCodec codec,
                          LiveStreamSettings settings, Action<string> log, Action<string> onInput)
    {
        _codec = codec;
        _settings = settings;
        _log = log;
        _onInput = onInput;
        // Longer than the hub's long-poll wait (8 s) with room for a slow upload of a 4K
        // keyframe batch on a poor uplink.
        _http = new HttpClient { Timeout = TimeSpan.FromSeconds(60) };
        _http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", bearer);
        var enc = Uri.EscapeDataString(sessionId);
        _downUrl = $"{AgentConfig.HubBase}/api/agent/remote/{enc}/relay/down";
        _upUrl = $"{AgentConfig.HubBase}/api/agent/remote/{enc}/relay/up";
    }

    /// <summary>Start uploading and polling. Asks for a keyframe first: the viewer has nothing
    /// to decode until one arrives.</summary>
    public void Start()
    {
        _settings.RequestKeyframe();
        _ = Task.Run(() => UploadLoopAsync(_cts.Token), _cts.Token);
        _ = Task.Run(() => InputLoopAsync(_cts.Token), _cts.Token);
        _log("hub relay started: frames go up to the hub, input comes back from it");
    }

    public void SendFrame(byte[] encoded, uint durationRtpUnits)
    {
        if (encoded.Length == 0 || Volatile.Read(ref _ended) == 1) return;
        bool key = RelayFraming.IsKeyframe(_codec, encoded);
        lock (_gate)
        {
            if (!key && _waitingForKey) { _framesDropped++; return; }
            if (_queuedFrames >= MaxQueuedFrames)
            {
                // Behind by seconds: drop every queued frame, keep the status messages, and start
                // again from a keyframe. See the class remarks.
                int dropped = _queue.RemoveAll(r => r.Kind == RelayFraming.KindVideo);
                _framesDropped += dropped;
                _queuedFrames = 0;
                if (!key)
                {
                    _waitingForKey = true;
                    _framesDropped++;
                    _settings.RequestKeyframe();
                    return;
                }
            }
            _waitingForKey = false;
            _queue.Add((RelayFraming.KindVideo, key ? RelayFraming.FlagKeyframe : (byte)0, encoded));
            _queuedFrames++;
        }
        _pending.Release();
    }

    public void SendControl(string json)
    {
        if (Volatile.Read(ref _ended) == 1) return;
        lock (_gate)
        {
            if (_queuedControl >= MaxQueuedControl) return;
            _queue.Add((RelayFraming.KindControl, 0, Encoding.UTF8.GetBytes(json)));
            _queuedControl++;
        }
        _pending.Release();
    }

    private async Task UploadLoopAsync(CancellationToken ct)
    {
        bool firstAccepted = false;
        while (!ct.IsCancellationRequested)
        {
            try { await _pending.WaitAsync(ct); }
            catch (OperationCanceledException) { return; }

            List<(byte Kind, byte Flags, byte[] Payload)> batch;
            lock (_gate)
            {
                if (_queue.Count == 0) continue;
                batch = new List<(byte, byte, byte[])>(_queue);
                _queue.Clear();
                _queuedFrames = 0;
                _queuedControl = 0;
            }
            // Every queued item released the semaphore once; this batch consumed them all.
            while (_pending.CurrentCount > 0)
            {
                if (!await _pending.WaitAsync(0, ct)) break;
            }

            var body = RelayFraming.Frame(batch);
            try
            {
                using var content = new ByteArrayContent(body);
                content.Headers.ContentType = new MediaTypeHeaderValue("application/octet-stream");
                using var resp = await _http.PostAsync(_downUrl, content, ct);
                if (resp.StatusCode is HttpStatusCode.Conflict or HttpStatusCode.NotFound)
                {
                    End($"hub refused relay upload ({(int)resp.StatusCode})");
                    return;
                }
                if (!resp.IsSuccessStatusCode)
                {
                    _log($"hub relay upload failed: HTTP {(int)resp.StatusCode}; re-keying");
                    LoseBatch(batch);
                    await Delay(ct);
                    continue;
                }
                _bytesSent += body.Length;
                _framesSent += batch.Count(r => r.Kind == RelayFraming.KindVideo);
                if (!firstAccepted)
                {
                    firstAccepted = true;
                    _log($"hub relay: first upload accepted ({body.Length} bytes)");
                }
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { return; }
            catch (Exception e)
            {
                _log($"hub relay upload failed: {e.Message}; re-keying");
                LoseBatch(batch);
                await Delay(ct);
            }
        }
    }

    /// <summary>A batch that never reached the hub leaves a hole in the stream the viewer
    /// cannot decode across, so wait for (and ask for) the next keyframe rather than sending
    /// deltas that reference frames the viewer never got.</summary>
    private void LoseBatch(List<(byte Kind, byte Flags, byte[] Payload)> batch)
    {
        lock (_gate)
        {
            _framesDropped += batch.Count(r => r.Kind == RelayFraming.KindVideo);
            int queued = _queue.RemoveAll(r => r.Kind == RelayFraming.KindVideo);
            _framesDropped += queued;
            _queuedFrames = 0;
            _waitingForKey = true;
        }
        _settings.RequestKeyframe();
    }

    private async Task InputLoopAsync(CancellationToken ct)
    {
        int after = 0;
        while (!ct.IsCancellationRequested)
        {
            try
            {
                using var resp = await _http.GetAsync($"{_upUrl}?after={after}", ct);
                if (resp.StatusCode == HttpStatusCode.NotFound)
                {
                    End("hub no longer knows this session");
                    return;
                }
                if (!resp.IsSuccessStatusCode) { await Delay(ct); continue; }
                var body = await resp.Content.ReadFromJsonAsync<UpResult>(cancellationToken: ct);
                if (body is null) { await Delay(ct); continue; }
                foreach (var message in body.Messages)
                    if (!string.IsNullOrEmpty(message)) _onInput(message);
                if (body.Next > after) after = body.Next;
                if (body.State == "closed")
                {
                    End("hub closed the relay");
                    return;
                }
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { return; }
            catch (Exception e)
            {
                _log($"hub relay input poll failed: {e.Message}");
                await Delay(ct);
            }
        }
    }

    private void End(string reason)
    {
        if (Interlocked.Exchange(ref _ended, 1) == 1) return;
        _log($"hub relay ended: {reason}");
        Ended?.Invoke(reason);
    }

    private static async Task Delay(CancellationToken ct)
    {
        try { await Task.Delay(RetryDelayMs, ct); }
        catch (OperationCanceledException) { /* shutting down: the caller's loop sees ct and exits */ }
    }

    public void Dispose()
    {
        Interlocked.Exchange(ref _ended, 1);
        _cts.Cancel();
        _log($"hub relay closed: {_framesSent} frame(s) and {_bytesSent / 1024} KB sent, " +
             $"{_framesDropped} frame(s) dropped to stay current");
        _http.Dispose();
        _cts.Dispose();
    }

    private sealed class UpResult
    {
        [JsonPropertyName("messages")] public List<string> Messages { get; set; } = new();
        [JsonPropertyName("next")] public int Next { get; set; }
        [JsonPropertyName("state")] public string State { get; set; } = "";
    }
}

/// <summary>
/// hub/remote_relay.py's record framing: <c>[kind:1][flags:1][length:4 big-endian][payload]</c>,
/// concatenated. Kept beside the client rather than inside it so the tests can pin the byte
/// layout the hub parses -- a mismatch here is a relay that answers 400 to every upload.
/// </summary>
public static class RelayFraming
{
    public const byte KindVideo = 1;
    public const byte KindControl = 2;
    public const byte FlagKeyframe = 0x01;

    public static byte[] Frame(IReadOnlyList<(byte Kind, byte Flags, byte[] Payload)> records)
    {
        int size = 0;
        foreach (var r in records) size += 6 + r.Payload.Length;
        var buffer = new byte[size];
        int offset = 0;
        foreach (var (kind, flags, payload) in records)
        {
            buffer[offset] = kind;
            buffer[offset + 1] = flags;
            System.Buffers.Binary.BinaryPrimitives.WriteUInt32BigEndian(
                buffer.AsSpan(offset + 2, 4), (uint)payload.Length);
            payload.CopyTo(buffer, offset + 6);
            offset += 6 + payload.Length;
        }
        return buffer;
    }

    /// <summary>True if <paramref name="frame"/> can start a decode on its own.
    ///
    /// H.264: an access unit carrying an IDR or a parameter set -- the encoder's own
    /// <see cref="H264Encoder.ContainsIdr"/>, so the relay and the encoder's keyframe watchdog
    /// cannot disagree about what a keyframe is. VP8: bit 0 of the frame tag is 0 on a keyframe
    /// (RFC 6386, section 9.1).</summary>
    public static bool IsKeyframe(VideoCodec codec, byte[] frame)
    {
        if (frame.Length == 0) return false;
        return codec == VideoCodec.Vp8 ? (frame[0] & 0x01) == 0 : H264Encoder.ContainsIdr(frame);
    }
}
