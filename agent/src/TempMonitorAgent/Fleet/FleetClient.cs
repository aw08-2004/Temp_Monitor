using System.Net;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging;
using TempMonitorAgent.State;

namespace TempMonitorAgent.Fleet;

/// <summary>
/// Client for the hub's fleet command channel. Enrolls once (persisting the returned
/// agent_id/token), then heartbeats, polls+claims commands, and reports results — all
/// authenticated with "Authorization: Bearer &lt;agent_id&gt;:&lt;token&gt;".
/// </summary>
public sealed class FleetClient : IDisposable
{
    private readonly ILogger<FleetClient> _log;
    private readonly AgentState _state;
    private readonly HttpClient _http;
    private AgentIdentity _identity;

    public FleetClient(ILogger<FleetClient> log, AgentState state)
    {
        _log = log;
        _state = state;
        _identity = state.LoadIdentity();
        _http = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
    }

    public bool IsEnrolled => _identity.IsEnrolled;
    public string AgentId => _identity.AgentId;

    /// <summary>Enroll if not already enrolled. Returns true when enrolled (now or before).</summary>
    public async Task<bool> EnsureEnrolledAsync(string? enrollmentSecret, CancellationToken ct)
    {
        if (_identity.IsEnrolled) return true;

        if (string.IsNullOrEmpty(enrollmentSecret))
        {
            _log.LogWarning("Not enrolled and no enrollment secret available; skipping fleet channel");
            return false;
        }

        var body = new JsonObject
        {
            ["machine"] = AgentConfig.MachineName,
            ["enrollment_secret"] = enrollmentSecret,
        };
        using var content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json");

        try
        {
            using var resp = await _http.PostAsync(AgentConfig.EnrollUrl, content, ct);
            var text = await resp.Content.ReadAsStringAsync(ct);
            if (!resp.IsSuccessStatusCode)
            {
                _log.LogWarning("Enrollment rejected ({Status}): {Body}", (int)resp.StatusCode, text);
                return false;
            }

            var json = JsonNode.Parse(text);
            var agentId = json?["agent_id"]?.GetValue<string>();
            var token = json?["token"]?.GetValue<string>();
            if (string.IsNullOrEmpty(agentId) || string.IsNullOrEmpty(token))
            {
                _log.LogWarning("Enrollment response missing agent_id/token");
                return false;
            }

            _identity = new AgentIdentity { AgentId = agentId, Token = token };
            _state.SaveIdentity(_identity);
            _log.LogInformation("Enrolled as agent {AgentId}", agentId);
            return true;
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException)
        {
            _log.LogWarning("Enrollment call failed: {Msg}", e.Message);
            return false;
        }
    }

    public async Task<bool> HeartbeatAsync(CancellationToken ct)
    {
        if (!_identity.IsEnrolled) return false;
        try
        {
            using var req = Authorized(HttpMethod.Post, AgentConfig.HeartbeatUrl);
            using var resp = await _http.SendAsync(req, ct);
            return resp.IsSuccessStatusCode;
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException)
        {
            _log.LogDebug("Heartbeat failed: {Msg}", e.Message);
            return false;
        }
    }

    /// <summary>Poll + claim pending commands. Returns an empty list on any failure.</summary>
    public async Task<List<FleetCommand>> PollCommandsAsync(CancellationToken ct)
    {
        if (!_identity.IsEnrolled) return new List<FleetCommand>();
        try
        {
            using var req = Authorized(HttpMethod.Get, AgentConfig.CommandsUrl);
            using var resp = await _http.SendAsync(req, ct);
            if (!resp.IsSuccessStatusCode) return new List<FleetCommand>();

            var text = await resp.Content.ReadAsStringAsync(ct);
            var parsed = JsonSerializer.Deserialize<CommandsResponse>(text);
            return parsed?.Commands ?? new List<FleetCommand>();
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException)
        {
            _log.LogDebug("Command poll failed: {Msg}", e.Message);
            return new List<FleetCommand>();
        }
    }

    /// <summary>Report a command's result. Must run under the same agent_id that claimed it.</summary>
    public async Task<bool> ReportResultAsync(string commandId, CommandResult result, CancellationToken ct)
    {
        if (!_identity.IsEnrolled) return false;
        var body = new JsonObject
        {
            ["success"] = result.Success,
            ["output"] = result.Output,
        };
        try
        {
            using var req = Authorized(HttpMethod.Post, AgentConfig.CommandResultUrl(commandId));
            req.Content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json");
            using var resp = await _http.SendAsync(req, ct);
            if (!resp.IsSuccessStatusCode)
            {
                var text = await resp.Content.ReadAsStringAsync(ct);
                _log.LogWarning("Result report for {Id} rejected ({Status}): {Body}",
                    commandId, (int)resp.StatusCode, text);
            }
            return resp.IsSuccessStatusCode;
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException)
        {
            _log.LogWarning("Result report for {Id} failed: {Msg}", commandId, e.Message);
            return false;
        }
    }

    private HttpRequestMessage Authorized(HttpMethod method, string url)
    {
        var req = new HttpRequestMessage(method, url);
        req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", _identity.BearerValue);
        return req;
    }

    private sealed class CommandsResponse
    {
        [System.Text.Json.Serialization.JsonPropertyName("commands")]
        public List<FleetCommand>? Commands { get; set; }
    }

    public void Dispose() => _http.Dispose();
}
