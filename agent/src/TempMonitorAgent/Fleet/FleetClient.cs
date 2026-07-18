using System.Net;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging;
using TempMonitorAgent.State;

namespace TempMonitorAgent.Fleet;

/// <summary>Outcome of posting one output chunk. <c>Truncated</c> is the hub telling us
/// the per-command output cap is reached and to stop streaming this command.</summary>
public readonly record struct OutputPostResult(bool Ok, bool Truncated);

/// <summary>The one call OutputStreamer needs from FleetClient. Exists as an interface
/// purely so the streamer's sequencing/retry/coalescing logic can be tested against a
/// fake — FleetClient itself owns a real HttpClient and isn't otherwise substitutable.</summary>
public interface IOutputSink
{
    Task<OutputPostResult> PostOutputAsync(string commandId, int seq, string text, CancellationToken ct);
}

/// <summary>
/// Client for the hub's fleet command channel. Enrolls once (persisting the returned
/// agent_id/token), then heartbeats, polls+claims commands, streams live output, and
/// reports results — all authenticated with
/// "Authorization: Bearer &lt;agent_id&gt;:&lt;token&gt;".
/// </summary>
public sealed class FleetClient : IDisposable, IOutputSink
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

    /// <summary>
    /// Liveness ping, and the channel the hub delivers operational config over.
    ///
    /// We send the config_version we currently hold; the hub includes a config payload
    /// only when it differs, so the steady-state 10-second heartbeat stays tiny. Config
    /// rides here rather than on /api/report because this endpoint is authenticated —
    /// /api/report deliberately is not.
    /// </summary>
    public async Task<bool> HeartbeatAsync(CancellationToken ct)
    {
        if (!_identity.IsEnrolled) return false;
        try
        {
            using var req = Authorized(HttpMethod.Post, AgentConfig.HeartbeatUrl);
            var known = RuntimeConfigStore.Current.ConfigVersion;
            req.Content = new StringContent(
                JsonSerializer.Serialize(new Dictionary<string, string> { ["config_version"] = known }),
                Encoding.UTF8, "application/json");

            using var resp = await _http.SendAsync(req, ct);
            if (!resp.IsSuccessStatusCode) return false;

            var text = await resp.Content.ReadAsStringAsync(ct);
            ApplyConfigFromHeartbeat(text);
            return true;
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException)
        {
            _log.LogDebug("Heartbeat failed: {Msg}", e.Message);
            return false;
        }
    }

    /// <summary>Apply a config payload if the heartbeat carried one. Fails soft: a
    /// malformed payload leaves the running config untouched, because losing sensor
    /// selection is worse than ignoring one bad push.</summary>
    private void ApplyConfigFromHeartbeat(string body)
    {
        try
        {
            var root = JsonNode.Parse(body)?.AsObject();
            if (root is null) return;
            if (!root.TryGetPropertyValue("config", out var configNode) || configNode is null) return;

            var version = root["config_version"]?.GetValue<string>() ?? "";
            var payload = new Dictionary<string, object?>();
            foreach (var (key, value) in configNode.AsObject())
            {
                payload[key] = value is JsonArray arr
                    ? arr.Select(n => (object?)n?.ToString()).ToList()
                    : value?.ToString();
            }

            var applied = RuntimeConfigStore.Current.Apply(payload, version);
            RuntimeConfigStore.Set(applied);
            _state.SaveRuntimeConfig(applied);
            _log.LogInformation("Applied hub config {Version}: sensors [{Sensors}]",
                version, string.Join(", ", applied.PreferredSensors));
        }
        catch (Exception e)
        {
            _log.LogWarning("Ignoring malformed hub config: {Msg}", e.Message);
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

    /// <summary>Post one chunk of a running command's output. Only OutputStreamer should
    /// call this — it owns the sequence numbering that makes retries idempotent.
    ///
    /// A 403 means the hub considers the command finished (or not ours); there is no
    /// point retrying, so report Ok with Truncated so the streamer stops cleanly rather
    /// than burning its retry budget. A pre-1.10 hub has no such endpoint and 404s —
    /// same treatment, which is what makes a new agent degrade gracefully to one-shot
    /// output against an old hub instead of failing the command.</summary>
    public async Task<OutputPostResult> PostOutputAsync(
        string commandId, int seq, string text, CancellationToken ct)
    {
        if (!_identity.IsEnrolled) return new OutputPostResult(Ok: false, Truncated: false);

        var body = new JsonObject { ["seq"] = seq, ["chunk"] = text };
        try
        {
            using var req = Authorized(HttpMethod.Post, AgentConfig.CommandOutputUrl(commandId));
            req.Content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json");
            using var resp = await _http.SendAsync(req, ct);

            if (resp.StatusCode is HttpStatusCode.Forbidden or HttpStatusCode.NotFound)
            {
                _log.LogDebug("Output for {Id} not accepted ({Status}); stopping stream",
                    commandId, (int)resp.StatusCode);
                return new OutputPostResult(Ok: true, Truncated: true);
            }
            if (!resp.IsSuccessStatusCode)
                return new OutputPostResult(Ok: false, Truncated: false);

            var json = JsonNode.Parse(await resp.Content.ReadAsStringAsync(ct));
            var truncated = json?["truncated"]?.GetValue<bool>() ?? false;
            return new OutputPostResult(Ok: true, Truncated: truncated);
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException)
        {
            _log.LogDebug("Output post for {Id} seq {Seq} failed: {Msg}", commandId, seq, e.Message);
            return new OutputPostResult(Ok: false, Truncated: false);
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
            ["cwd"] = result.Cwd,
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
