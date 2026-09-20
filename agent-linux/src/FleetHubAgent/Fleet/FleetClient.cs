using System.Net;
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
/// unattended loops on a machine nobody is looking at; an exception escaping one of them
/// takes down the loop that called it, and a dead heartbeat loop is a machine that reads
/// offline in the console while running perfectly well. So a hub that is down, slow, or
/// answering with nonsense is a false return value here, never an exception.
///
/// This is a deliberately SMALL subset of the Windows agent's FleetClient (which is ~1150
/// lines): enroll, heartbeat, poll, result, and streamed command output. The rest of that
/// surface -- the ConPTY terminal endpoints, package downloads, backup and BIOS reporting --
/// backs features this agent does not implement, and every one of them is gated in the
/// console behind a MIN_*_AGENT version this agent deliberately sits below (see
/// AgentConfig.Version). Porting an endpoint before its feature exists would be dead code
/// carrying a bearer token.
/// </summary>
public sealed class FleetClient : IDisposable, IOutputSink
{
    private readonly ILogger<FleetClient> _log;
    private readonly AgentState _state;
    private readonly HttpClient _http;

    /// <summary>Held command polls get their own client: the 10-second budget on _http exists
    /// to notice a hub that has stopped answering, and a request we explicitly asked the hub
    /// to sit on until it has something is not that.</summary>
    private readonly HttpClient _commandHttp;

    private AgentIdentity _identity;

    /// <summary>What this agent claims it can do, asked fresh on every heartbeat.
    ///
    /// A FUNCTION rather than a value because the dispatcher that answers for it is built
    /// after this client -- and because capturing the report once would freeze it, which is
    /// exactly the drift AgentCapabilities exists to avoid. Null on a build that does not
    /// pass one, in which case the heartbeat carries no capabilities block at all and the hub
    /// reads this machine as "has not said", which is what it did before this existed.</summary>
    private readonly Func<AgentCapabilities>? _capabilities;

    public FleetClient(ILogger<FleetClient> log, AgentState state,
                       Func<AgentCapabilities>? capabilities = null)
    {
        _log = log;
        _state = state;
        _capabilities = capabilities;
        _identity = state.LoadIdentity();
        _http = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
        _commandHttp = new HttpClient
        {
            Timeout = TimeSpan.FromSeconds(AgentConfig.CommandPollTimeoutSeconds),
        };
    }

    public bool IsEnrolled => _identity.IsEnrolled;

    /// <summary>Enroll if we have not already, using the shared secret the installer left
    /// behind. Returns whether the agent now has an identity.
    ///
    /// The caller serialises this -- see Worker's enroll gate. Enrolling twice would mint a
    /// second identity for one machine and duplicate it in the fleet.</summary>
    public async Task<bool> EnsureEnrolledAsync(string? enrollmentSecret, CancellationToken ct)
    {
        if (_identity.IsEnrolled) return true;

        if (string.IsNullOrEmpty(enrollmentSecret))
        {
            _log.LogWarning("Not enrolled and no enrollment secret available; " +
                            "staying on telemetry only");
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

            // Persist BEFORE adopting it in memory. The token comes back exactly once; if the
            // write fails we would rather this attempt look like a failure and be retried
            // than run on an identity that will be gone at the next restart.
            var identity = new AgentIdentity { AgentId = agentId, Token = token };
            _state.SaveIdentity(identity);
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
    /// Liveness ping.
    ///
    /// The Windows agent's heartbeat also carries the change-only inventory blocks (backup
    /// profiles, network adapters, BIOS settings, the process list) and applies the config the
    /// hub replies with. Of those only the patch inventory is ported, so this sends the
    /// minimum the endpoint accepts plus the capability report and whatever
    /// <paramref name="patches"/> carries, and reads nothing back but the status code.
    ///
    /// <paramref name="patches"/> is passed IN rather than pulled from a reporter held here,
    /// which keeps this class ignorant of when a payload may be considered delivered. The
    /// caller knows: it is the one that can see whether this call returned true. See
    /// PatchInventoryReporter, where losing that distinction costs the hub the one report it
    /// most needs.
    ///
    /// It still has to EXIST, and on the Windows cadence: this is what refreshes the hub's
    /// last_seen, and the console calls a machine offline after 90 seconds without one.
    /// </summary>
    public async Task<bool> HeartbeatAsync(CancellationToken ct, JsonObject? patches = null)
    {
        if (!_identity.IsEnrolled) return false;
        try
        {
            using var req = Authorized(HttpMethod.Post, AgentConfig.HeartbeatUrl);
            // An empty config_version says "I hold no config", which is true and is what a
            // freshly enrolled Windows agent sends too. The hub answers with a config block
            // we currently ignore; ignoring it is honest, since nothing here reads a
            // preferred-sensor list yet.
            var body = new JsonObject { ["config_version"] = "" };
            // What this machine is and what it can be told to do. The hub keys a release
            // manifest, an outdated tally and a set of console gates on the platform in here,
            // so a heartbeat that omitted it would leave this machine measured against the
            // WINDOWS agent's train -- which is the arrangement AgentConfig.Version's low
            // number used to work around. Built per heartbeat rather than captured; see
            // _capabilities.
            try
            {
                if (_capabilities is not null) body["capabilities"] = _capabilities().ToJson();
            }
            catch (Exception e)
            {
                // Never fatal, and never at the cost of the heartbeat itself: this ping is
                // what decides whether the machine reads online, and a capability report is a
                // hint. Same discipline the hub applies at the other end of the wire.
                _log.LogDebug("Could not build the capability report: {Msg}", e.Message);
            }
            // What this machine is missing (roadmap #14). Sent only when the scan produced
            // something the hub has not been told yet -- and DeepClone'd, because a JsonNode
            // may have only one parent: attaching the reporter's own object here would
            // re-parent it, and a heartbeat that then failed would leave the reporter holding
            // a node it can no longer serialise for the retry.
            if (patches is not null) body["patches"] = patches.DeepClone();
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

    /// <summary>Post one chunk of a running command's output.
    ///
    /// **Every failure here is soft, and the two kinds are told apart deliberately.** A 403 or
    /// a 404 means the hub will never accept another chunk for this command -- it completed,
    /// or it was never ours -- so that is reported as Truncated, which stops the stream
    /// rather than retrying into a wall. Anything else is a transport hiccup the caller may
    /// retry with the SAME seq, which the hub's INSERT OR IGNORE makes free.
    ///
    /// Note the order of the two reads at the end: the hub answers 200 with
    /// {"truncated": true} once it has stored all it will store for one command, and that is
    /// a successful post which also ends the stream. Treating it as a failure would retry a
    /// chunk the hub deliberately discarded.</summary>
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
