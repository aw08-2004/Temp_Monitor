using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace TempMonitorAgent.Remote;

/// <summary>
/// The helper's side of WebRTC signaling (roadmap #2): posts this peer's SDP offer and ICE
/// candidates to the hub, and polls for the console's answer + ICE. Authenticated with the
/// agent's enrollment bearer token (loaded from agent.json), talking to the same hub the
/// service reports to. Plain HTTP polling -- the agent has no listening port and needs none;
/// signaling is a short burst at setup.
/// </summary>
public sealed class RemoteSignalingClient : IDisposable
{
    private readonly HttpClient _http;
    private readonly string _signalUrl;
    private readonly string _pollUrl;
    private readonly string _endedUrl;

    public RemoteSignalingClient(string sessionId, string bearer)
    {
        _http = new HttpClient { Timeout = TimeSpan.FromSeconds(30) };
        _http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", bearer);
        var baseUrl = AgentConfig.HubBase;
        var enc = Uri.EscapeDataString(sessionId);
        _signalUrl = $"{baseUrl}/api/agent/remote/{enc}/signal";
        _pollUrl = $"{baseUrl}/api/agent/remote/{enc}/poll";
        _endedUrl = $"{baseUrl}/api/agent/remote/{enc}/ended";
    }

    /// <summary>Send one signal (offer/ice/bye) to the console side.</summary>
    public async Task PostSignalAsync(string kind, object payload, CancellationToken ct)
    {
        using var resp = await _http.PostAsJsonAsync(
            _signalUrl, new { kind, payload }, ct);
        resp.EnsureSuccessStatusCode();
    }

    /// <summary>Fetch console signals newer than <paramref name="afterSeq"/>, plus the session
    /// status so the helper knows when to tear down.</summary>
    public async Task<PollResult> PollAsync(int afterSeq, CancellationToken ct)
    {
        using var resp = await _http.GetAsync($"{_pollUrl}?after_seq={afterSeq}", ct);
        resp.EnsureSuccessStatusCode();
        var result = await resp.Content.ReadFromJsonAsync<PollResult>(cancellationToken: ct);
        return result ?? new PollResult();
    }

    /// <summary>Tell the hub this session has ended (consent denied, capture failed, or normal
    /// teardown), so it doesn't sit alive until the TTL sweep. Best-effort.</summary>
    public async Task ReportEndedAsync(string reason, CancellationToken ct)
    {
        using var resp = await _http.PostAsJsonAsync(_endedUrl, new { reason }, ct);
        resp.EnsureSuccessStatusCode();
    }

    public void Dispose() => _http.Dispose();

    public sealed class PollResult
    {
        [JsonPropertyName("signals")] public List<SignalMessage> Signals { get; set; } = new();
        [JsonPropertyName("next_seq")] public int NextSeq { get; set; }
        [JsonPropertyName("status")] public string Status { get; set; } = "";
    }

    public sealed class SignalMessage
    {
        [JsonPropertyName("seq")] public int Seq { get; set; }
        [JsonPropertyName("kind")] public string Kind { get; set; } = "";
        // Left as a JsonElement: an answer carries {sdp}, an ice candidate carries the WebRTC
        // candidate init shape -- the peer decodes each based on Kind.
        [JsonPropertyName("payload")] public JsonElement Payload { get; set; }
    }
}
