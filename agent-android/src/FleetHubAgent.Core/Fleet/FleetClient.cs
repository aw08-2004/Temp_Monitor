using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.Json.Serialization;
using Microsoft.Extensions.Logging;
using FleetHubAgent.State;

namespace FleetHubAgent.Fleet;

/// <summary>
/// Client for the hub's fleet command channel: enroll once (persisting the returned
/// agent_id/token), then heartbeat, poll and claim commands, and report results -- all
/// authenticated with "Authorization: Bearer &lt;agent_id&gt;:&lt;token&gt;".
///
/// **Every method here fails soft and returns rather than throwing.** These are called from
/// unattended loops on a device nobody is looking at; an exception escaping one of them takes
/// down the loop that called it, and a dead heartbeat loop is a machine that reads offline in
/// the console while running perfectly well. So a hub that is down, slow, or answering with
/// nonsense is a false return value here, never an exception. On Android that matters more
/// than elsewhere: a phone changes network several times a day, so the failure path is the
/// ordinary path.
///
/// This is the same deliberately SMALL subset as the Linux agent's: enroll, heartbeat, poll,
/// result. The rest of the Windows agent's surface -- live output streaming, the ConPTY
/// terminal endpoints, package downloads, backup and BIOS reporting -- backs features this
/// agent does not implement, and every one of them is gated in the console behind a
/// MIN_*_AGENT version this agent deliberately sits below (see AgentConfig.Version). Porting
/// an endpoint before its feature exists would be dead code carrying a bearer token.
/// </summary>
public sealed class FleetClient : IDisposable
{
    private readonly ILogger<FleetClient> _log;
    private readonly AgentState _state;
    private readonly MachineNameProvider _names;
    private readonly HttpClient _http;

    /// <summary>Held command polls get their own client: the 10-second budget on _http exists
    /// to notice a hub that has stopped answering, and a request we explicitly asked the hub
    /// to sit on until it has something is not that.</summary>
    private readonly HttpClient _commandHttp;

    /// <summary>What this device tells the hub it can do, sent on every heartbeat. Optional so
    /// a caller that has no dispatcher to derive it from still gets a working client -- the hub
    /// reads a missing block as "has not said" and gates nothing, which is exactly the state
    /// every Windows agent in the field is in.</summary>
    private readonly AgentCapabilities? _capabilities;

    private AgentIdentity _identity;

    public FleetClient(ILogger<FleetClient> log, AgentState state, MachineNameProvider names,
        AgentCapabilities? capabilities = null)
    {
        _log = log;
        _state = state;
        _names = names;
        _capabilities = capabilities;
        _identity = state.LoadIdentity();
        _http = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
        _commandHttp = new HttpClient
        {
            Timeout = TimeSpan.FromSeconds(AgentConfig.CommandPollTimeoutSeconds),
        };
    }

    public bool IsEnrolled => _identity.IsEnrolled;

    /// <summary>Enroll if we have not already, using the shared secret the operator or the MDM
    /// supplied. Returns whether the agent now has an identity.
    ///
    /// The caller serialises this -- see the enroll gate in AgentLoops. Enrolling twice would
    /// mint a second identity for one device and duplicate it in the fleet.</summary>
    public async Task<bool> EnsureEnrolledAsync(string? enrollmentSecret, CancellationToken ct)
    {
        if (_identity.IsEnrolled) return true;

        if (string.IsNullOrEmpty(enrollmentSecret))
        {
            // Debug, not Warning, and that differs from the Linux agent on purpose. The caller
            // retries every 30 seconds forever, so at Warning this is one line per half-minute
            // for the entire life of a device that has not been given a secret yet -- and
            // logcat is a ring buffer, so on Android that steadily evicts everything else the
            // agent has said. AgentLoops already reports the same condition ONCE at Warning,
            // which is the line an operator needs; this one is only useful when following the
            // enrollment path live. Observed on the first real device.
            _log.LogDebug("Not enrolled and no enrollment secret available; " +
                          "staying on telemetry only");
            return false;
        }

        var body = new JsonObject
        {
            ["machine"] = _names.Current,
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

            // Persist BEFORE adopting it in memory, and treat a failed write as a failed
            // enrollment. The token comes back exactly once; running on an identity that is
            // not on disk works until Android kills the process, and then this device enrolls
            // again and appears in the console as a second machine. Reporting failure here
            // costs one retry in thirty seconds.
            var identity = new AgentIdentity { AgentId = agentId, Token = token };
            if (!_state.SaveIdentity(identity))
            {
                _log.LogError("Enrolled as {AgentId} but could not store the identity; " +
                              "discarding it and retrying rather than running on a token " +
                              "that will not survive a restart.", agentId);
                return false;
            }
            _identity = identity;
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
    /// Liveness ping, and the one thing this agent tells the hub about itself.
    ///
    /// The Windows agent's heartbeat also carries the change-only inventory blocks (backup
    /// profiles, network adapters, BIOS settings, available patches, the process list) and
    /// applies the config the hub replies with. None of that is ported, so this sends the
    /// minimum the endpoint accepts plus the capability block, and reads nothing back but the
    /// status code.
    ///
    /// It still has to EXIST, and on the same cadence as every other agent: this is what
    /// refreshes the hub's last_seen, and the console calls a machine offline after 90 seconds
    /// without one.
    ///
    /// **The capability block rides here rather than on enrollment**, which is the obvious
    /// alternative and the wrong one: enrollment happens once, and a hub that lost its database
    /// (or a device whose agent gained an executor in an update) would never be told again. A
    /// heartbeat is the one message that repeats forever, so the report is self-healing. It
    /// costs a hundred bytes every ten seconds; the hub writes only when the content differs.
    /// </summary>
    public async Task<bool> HeartbeatAsync(CancellationToken ct)
    {
        if (!_identity.IsEnrolled) return false;
        try
        {
            using var req = Authorized(HttpMethod.Post, AgentConfig.HeartbeatUrl);
            // An empty config_version says "I hold no config", which is true and is what a
            // freshly enrolled Windows agent sends too. The hub answers with a config block we
            // currently ignore; ignoring it is honest, since nothing here reads a
            // preferred-sensor list yet.
            var body = new JsonObject { ["config_version"] = "" };
            if (_capabilities is not null) body["capabilities"] = _capabilities.ToJson();
            req.Content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json");

            using var resp = await _http.SendAsync(req, ct);
            return resp.IsSuccessStatusCode;
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException)
        {
            _log.LogDebug("Heartbeat failed: {Msg}", e.Message);
            return false;
        }
    }

    /// <summary>Poll for queued commands, asking the hub to hold the request open for
    /// <paramref name="waitSeconds"/> if it is willing.</summary>
    public async Task<CommandPollResult> PollCommandsAsync(int waitSeconds, CancellationToken ct)
    {
        if (!_identity.IsEnrolled) return CommandPollResult.Empty;
        var holding = waitSeconds > 0;
        var url = holding ? AgentConfig.CommandsUrl_Waiting(waitSeconds) : AgentConfig.CommandsUrl;
        try
        {
            using var req = Authorized(HttpMethod.Get, url);
            using var resp = await (holding ? _commandHttp : _http).SendAsync(req, ct);
            if (!resp.IsSuccessStatusCode) return CommandPollResult.Empty;

            var text = await resp.Content.ReadAsStringAsync(ct);
            var parsed = JsonSerializer.Deserialize<CommandsResponse>(text);
            return new CommandPollResult(
                parsed?.Commands ?? new List<FleetCommand>(),
                // Absent on a hub too old to know about push, which deserializes to false --
                // exactly right, since such a hub never held anything.
                parsed?.Waited ?? false);
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException)
        {
            _log.LogDebug("Command poll failed: {Msg}", e.Message);
            return CommandPollResult.Empty;
        }
    }

    /// <summary>Report a finished command. A rejection is logged with the hub's own body:
    /// this is the one place where the reason a command "did nothing" is visible, and
    /// swallowing it leaves an operator staring at a command stuck in "running".</summary>
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

    /// <summary>The command endpoint's envelope. Private because nothing outside this class
    /// should care that the list arrives wrapped.</summary>
    private sealed class CommandsResponse
    {
        [JsonPropertyName("commands")] public List<FleetCommand> Commands { get; set; } = new();
        [JsonPropertyName("waited")] public bool Waited { get; set; }
    }

    public void Dispose()
    {
        _http.Dispose();
        _commandHttp.Dispose();
    }
}
