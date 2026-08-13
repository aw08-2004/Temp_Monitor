using System.Net;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging;
using TempMonitorAgent.Backup;
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

/// <summary>One item the console queued for an open terminal: either raw bytes the operator
/// typed (<c>Kind == "data"</c>) or a terminal resize (<c>Kind == "resize"</c>).</summary>
public readonly record struct PtyInputItem(int Seq, string Kind, string Data, short Cols, short Rows);

/// <summary>Result of one keystroke poll. <c>Ok</c> is false when the hub couldn't be
/// reached at all — distinct from "reached it, nothing typed", because only the former
/// should make the session give up. <c>Closing</c> is the console asking us to shut the
/// terminal down.</summary>
public readonly record struct PtyInputBatch(
    bool Ok, IReadOnlyList<PtyInputItem> Items, int NextSeq, bool Closing, bool Gone);

/// <summary>The terminal half of the hub API, split out for the same reason as IOutputSink:
/// PtySessionRunner's polling/coalescing/backoff is worth testing without a live hub.</summary>
public interface IPtyChannel
{
    Task<PtyInputBatch> PullPtyInputAsync(string sessionId, int afterSeq, CancellationToken ct);
    Task<bool> PostPtyOutputAsync(string sessionId, int seq, string chunk, CancellationToken ct);
    Task ReportPtyClosedAsync(string sessionId, string reason, CancellationToken ct);
}

/// <summary>Fetching a package payload, for DeployPackageExecutor. An interface for the
/// same reason as IOutputSink: the executor's verify/run/detect logic is worth testing
/// without a live hub behind it.</summary>
public interface IPackageDownloader
{
    /// <summary>Download <paramref name="url"/> to <paramref name="destPath"/> and check
    /// its sha256. Returns null on success, or a human-readable failure reason.</summary>
    Task<string?> DownloadPackageAsync(string url, string destPath, string? expectedSha256,
                                       CancellationToken ct);
}

/// <summary>
/// Client for the hub's fleet command channel. Enrolls once (persisting the returned
/// agent_id/token), then heartbeats, polls+claims commands, streams live output, and
/// reports results — all authenticated with
/// "Authorization: Bearer &lt;agent_id&gt;:&lt;token&gt;".
/// </summary>
public sealed class FleetClient : IDisposable, IOutputSink, IPackageDownloader, IPtyChannel
{
    private readonly ILogger<FleetClient> _log;
    private readonly AgentState _state;
    private readonly HttpClient _http;
    // Package payloads are hundreds of megabytes on a slow link, so they cannot share
    // the 10-second client above — that timeout is a *whole request* budget, not an idle
    // one, and would abort every large installer mid-download.
    private readonly HttpClient _downloadHttp;
    // Held command polls, for the same reason downloads have their own client: the 10-second
    // budget above is there to notice a hub that has stopped answering, and a request we
    // explicitly asked the hub to sit on until it has something is not that.
    private readonly HttpClient _commandHttp;
    private AgentIdentity _identity;

    public FleetClient(ILogger<FleetClient> log, AgentState state)
    {
        _log = log;
        _state = state;
        _identity = state.LoadIdentity();
        _http = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
        _downloadHttp = new HttpClient { Timeout = TimeSpan.FromMinutes(30) };
        _commandHttp = new HttpClient
        {
            Timeout = TimeSpan.FromSeconds(AgentConfig.CommandPollTimeoutSeconds),
        };
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
            var body = new JsonObject { ["config_version"] = known };
            // Only when it has actually changed -- see BackupProfileReporter. Sending a
            // profile block on every 10-second heartbeat would be pure noise.
            var profiles = BackupProfileReporter.TakeIfChanged();
            if (profiles is not null) body["profiles"] = profiles;
            // Logon sessions + display outputs, for the remote session picker and the
            // "no display outputs" badge. Same change-only discipline as profiles.
            var remote = TempMonitorAgent.Remote.RemoteInventoryReporter.TakeIfChanged();
            if (remote is not null) body["remote"] = remote;
            // Firmware settings (roadmap #9). Same discipline again, and the payload that
            // benefits from it most: a BIOS enumeration takes seconds, so it is scanned on the
            // inventory loop and only CARRIED here. A machine with no manageable firmware
            // reports that once and then never appears in a heartbeat again.
            var biosInventory = TempMonitorAgent.Bios.BiosInventoryReporter.TakeIfChanged();
            if (biosInventory is not null) body["bios"] = biosInventory;
            // Network adapters (roadmap #10) -- MAC, address and prefix per adapter. This is
            // what lets the hub group machines into subnets and pick an awake peer to relay a
            // magic packet through; the only address the hub can see for itself is the NAT'd
            // site edge, which every machine at an office shares. Same change-only discipline,
            // so a settled desktop sends this once.
            var network = TempMonitorAgent.Network.NetworkInventoryReporter.TakeIfChanged();
            if (network is not null) body["network"] = network;
            // The process list, and the ONE payload here that is not change-only: a process
            // list has changed by definition. What bounds it instead is demand -- there is a
            // sample waiting only while the hub has told us somebody is looking (see
            // ProcessReporter and the `processes_wanted` reply below), so a machine nobody
            // has open contributes nothing to this body at all.
            var procs = TempMonitorAgent.Telemetry.ProcessReporter.TakeLatest();
            if (procs is not null) body["processes"] = procs;
            req.Content = new StringContent(body.ToJsonString(), Encoding.UTF8,
                                            "application/json");

            using var resp = await _http.SendAsync(req, ct);
            if (!resp.IsSuccessStatusCode) return false;

            var text = await resp.Content.ReadAsStringAsync(ct);
            ApplyConfigFromHeartbeat(text);
            ApplyProcessWatchFromHeartbeat(text);
            ApplyLiveWatchFromHeartbeat(text);
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

    /// <summary>
    /// Apply the hub's answer to "is anybody looking at this machine's processes?".
    ///
    /// Its own method with its own try/catch rather than a branch inside
    /// ApplyConfigFromHeartbeat, because the two must not be able to lose each other: a
    /// malformed config block would otherwise stop the watch flag being read, and a machine
    /// would sample forever (or never) on the strength of an unrelated parse failure.
    ///
    /// A hub too old to know about this feature sends no such field, which reads as false --
    /// the correct answer, since it has nowhere to put a process report either.
    /// </summary>
    private void ApplyProcessWatchFromHeartbeat(string body)
    {
        try
        {
            var wanted = JsonNode.Parse(body)?["processes_wanted"]?.GetValue<bool>() ?? false;
            TempMonitorAgent.Telemetry.ProcessReporter.SetWanted(wanted);
        }
        catch
        {
            // Deliberately does NOT clear the watch: a single unparseable reply is not the
            // hub saying "stop", and blanking an operator's live list over one bad response
            // would be a worse failure than sampling for one extra cycle.
        }
    }

    /// <summary>
    /// Apply the hub's answer to "is anybody watching this machine's charts?", which decides
    /// whether telemetry runs at one second or five (see LiveTelemetry).
    ///
    /// Its own method and its own try/catch for the same reason as the process watch above:
    /// two independent flags on one reply must not be able to lose each other to a parse
    /// failure in something unrelated.
    ///
    /// Unlike the process watch this one DOES fall back to false when the field is absent,
    /// and that is deliberate rather than an oversight: absent means a hub that predates the
    /// feature, and the correct cadence against such a hub is the one it has always received.
    /// A malformed reply is still left alone (see the catch).
    /// </summary>
    private void ApplyLiveWatchFromHeartbeat(string body)
    {
        try
        {
            var root = JsonNode.Parse(body);
            var wanted = root?["live_wanted"]?.GetValue<bool>() ?? false;
            var interval = root?["live_interval_seconds"]?.GetValue<int>();
            TempMonitorAgent.Telemetry.LiveTelemetry.SetWanted(wanted, interval);
        }
        catch
        {
            // One unreadable reply is not the hub saying "slow down"; the next heartbeat is
            // ten seconds away and will say so properly if it meant it.
        }
    }

    /// <summary>
    /// Ask the hub whether an operator is looking at this machine's processes, and apply the
    /// answer. Returns whether we are now wanted.
    ///
    /// **Why this exists next to a heartbeat that answers the same question.** The heartbeat
    /// ticks every 10 seconds and carries config, inventory and liveness with it; waiting for
    /// one before even starting to sample put the operator's first process list 10-15 seconds
    /// after the click that asked for it. This is the same question asked on its own, cheaply
    /// enough (see AgentConfig.ProcessIdleCheckSeconds) that it can be asked every couple of
    /// seconds by every machine in the fleet -- which is what makes the card feel like it
    /// responded rather than eventually caught up.
    ///
    /// **Failure leaves the watch exactly as it was**, and the caller sees the current state
    /// rather than a lie: a dropped request is not the hub saying "stop", and clearing the
    /// flag on one timeout would blank an operator's live list mid-read. A hub too old to
    /// serve this route 404s, which is the same story -- those machines fall back to learning
    /// from the heartbeat, which is exactly how this worked before.
    /// </summary>
    /// <summary>
    /// Ask the hub who is looking at this machine -- its process list, its live charts, or
    /// neither -- and apply both answers. Returns whether the process list is wanted, which
    /// is the one the caller's loop paces itself on.
    ///
    /// **One request for two watches.** Both questions have the same shape (a tiny indexed
    /// lookup answered every couple of seconds by every machine in the fleet), and asking
    /// them separately would exactly double the only traffic an unwatched machine generates
    /// for either feature.
    ///
    /// **A hub that predates /api/agent/watch 404s**, and this falls back to the older
    /// process-only route for it -- permanently would be wrong, since the hub is upgraded
    /// while these agents keep running, so the fallback is re-probed periodically. Such a hub
    /// has no live watch to report either, and the ordinary five-second telemetry it already
    /// expects is exactly what it keeps getting.
    /// </summary>
    public async Task<bool> PollWatchAsync(CancellationToken ct)
    {
        if (!_identity.IsEnrolled) return false;
        if (_watchRouteMissingUntil > DateTime.UtcNow)
            return await PollProcessWatchAsync(ct);

        try
        {
            using var req = Authorized(HttpMethod.Get, AgentConfig.WatchUrl);
            using var resp = await _http.SendAsync(req, ct);
            if (resp.StatusCode == HttpStatusCode.NotFound)
            {
                _watchRouteMissingUntil = DateTime.UtcNow.AddMinutes(WatchRouteRetryMinutes);
                return await PollProcessWatchAsync(ct);
            }
            if (!resp.IsSuccessStatusCode)
                return TempMonitorAgent.Telemetry.ProcessReporter.Wanted;

            var text = await resp.Content.ReadAsStringAsync(ct);
            var root = JsonNode.Parse(text);
            var procs = root?["processes"]?.GetValue<bool>() ?? false;
            TempMonitorAgent.Telemetry.ProcessReporter.SetWanted(procs);
            TempMonitorAgent.Telemetry.LiveTelemetry.SetWanted(
                root?["live"]?.GetValue<bool>() ?? false,
                root?["live_interval_seconds"]?.GetValue<int>());
            return procs;
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException)
        {
            _log.LogDebug("Watch poll failed: {Msg}", e.Message);
            return TempMonitorAgent.Telemetry.ProcessReporter.Wanted;
        }
        catch (Exception e)
        {
            // Same reasoning as the process-only poll below: neither watch is cleared over an
            // unreadable body, and a loop that can throw every two seconds is a loop that
            // fills the log.
            _log.LogDebug("Watch poll returned something unreadable: {Msg}", e.Message);
            return TempMonitorAgent.Telemetry.ProcessReporter.Wanted;
        }
    }

    /// <summary>How long a 404 on /api/agent/watch keeps us on the older route before trying
    /// again. Long enough that an old hub costs one extra request per agent per ten minutes,
    /// short enough that a hub upgraded under a running fleet is picked up the same shift --
    /// agents do not restart when the hub does.</summary>
    private const int WatchRouteRetryMinutes = 10;
    private DateTime _watchRouteMissingUntil = DateTime.MinValue;

    public async Task<bool> PollProcessWatchAsync(CancellationToken ct)
    {
        if (!_identity.IsEnrolled) return false;
        try
        {
            using var req = Authorized(HttpMethod.Get, AgentConfig.ProcessWatchUrl);
            using var resp = await _http.SendAsync(req, ct);
            if (!resp.IsSuccessStatusCode)
                return TempMonitorAgent.Telemetry.ProcessReporter.Wanted;

            var text = await resp.Content.ReadAsStringAsync(ct);
            var wanted = JsonNode.Parse(text)?["wanted"]?.GetValue<bool>() ?? false;
            TempMonitorAgent.Telemetry.ProcessReporter.SetWanted(wanted);
            return wanted;
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException)
        {
            _log.LogDebug("Process watch poll failed: {Msg}", e.Message);
            return TempMonitorAgent.Telemetry.ProcessReporter.Wanted;
        }
        catch (Exception e)
        {
            // A malformed body is not a reason to stop sampling either, and this runs on a
            // two-second loop -- one that can throw is one that fills the log.
            _log.LogDebug("Process watch poll returned something unreadable: {Msg}", e.Message);
            return TempMonitorAgent.Telemetry.ProcessReporter.Wanted;
        }
    }

    /// <summary>
    /// Poll + claim pending commands, optionally asking the hub to HOLD the request open
    /// while the queue is empty so a command issued meanwhile arrives at once.
    ///
    /// <paramref name="waitSeconds"/> of 0 is the plain poll this has always been. Returns
    /// the commands and whether the hub actually held the request — the caller uses that to
    /// decide whether to sleep before asking again, because a hold IS the sleep.
    ///
    /// Both failure paths report Waited=false on purpose. A hub that could not be reached,
    /// answered an error, or dropped the connection has told us nothing about whether it
    /// would have held, so the caller must fall back to its own cadence rather than
    /// reconnecting in a tight loop against something that is already unwell.
    /// </summary>
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

    // ---------------------------------------------------------------- interactive terminal
    // These three are the whole hub-facing surface of a ConPTY session. They are separate
    // from the command endpoints above because a terminal is a stream, not a command: it
    // has no result, no exit code worth recording, and a lifetime measured in an operator's
    // attention span. See hub/terminal.py for the other end.

    /// <summary>Fetch keystrokes and resizes the console queued for this session. Called on
    /// a tight loop while a terminal is open, so it must be cheap and must never throw.</summary>
    public async Task<PtyInputBatch> PullPtyInputAsync(string sessionId, int afterSeq, CancellationToken ct)
    {
        var empty = Array.Empty<PtyInputItem>();
        if (!_identity.IsEnrolled) return new PtyInputBatch(false, empty, afterSeq, false, false);

        try
        {
            using var req = Authorized(HttpMethod.Get, AgentConfig.PtyInputUrl(sessionId, afterSeq));
            using var resp = await _http.SendAsync(req, ct);

            // The hub forgot this session (reaped, or the DB was reset). Nothing will ever
            // arrive for it, so the session should end rather than poll a 404 forever.
            if (resp.StatusCode == HttpStatusCode.NotFound)
                return new PtyInputBatch(true, empty, afterSeq, Closing: true, Gone: true);
            if (!resp.IsSuccessStatusCode)
                return new PtyInputBatch(false, empty, afterSeq, false, false);

            var json = JsonNode.Parse(await resp.Content.ReadAsStringAsync(ct));
            var items = new List<PtyInputItem>();
            if (json?["items"] is JsonArray array)
            {
                foreach (var node in array)
                {
                    if (node is null) continue;
                    var seq = node["seq"]?.GetValue<int>() ?? -1;
                    var kind = node["kind"]?.GetValue<string>() ?? "data";
                    if (kind == "resize")
                    {
                        var size = node["size"];
                        items.Add(new PtyInputItem(seq, "resize", "",
                            (short)(size?["cols"]?.GetValue<int>() ?? 120),
                            (short)(size?["rows"]?.GetValue<int>() ?? 30)));
                    }
                    else
                    {
                        items.Add(new PtyInputItem(seq, "data", node["data"]?.GetValue<string>() ?? "", 0, 0));
                    }
                }
            }
            return new PtyInputBatch(
                true, items,
                json?["next_seq"]?.GetValue<int>() ?? afterSeq,
                json?["closing"]?.GetValue<bool>() ?? false,
                Gone: false);
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException)
        {
            _log.LogDebug("pty input poll for {Id} failed: {Msg}", sessionId, e.Message);
            return new PtyInputBatch(false, empty, afterSeq, false, false);
        }
    }

    /// <summary>Post one VT chunk. Like PostOutputAsync, a retry MUST reuse the same seq —
    /// the hub keys on it, and a fresh number would duplicate bytes into the middle of an
    /// escape sequence and corrupt the operator's screen.</summary>
    public async Task<bool> PostPtyOutputAsync(string sessionId, int seq, string chunk, CancellationToken ct)
    {
        if (!_identity.IsEnrolled) return false;
        var body = new JsonObject { ["seq"] = seq, ["chunk"] = chunk };
        try
        {
            using var req = Authorized(HttpMethod.Post, AgentConfig.PtyOutputUrl(sessionId));
            req.Content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json");
            using var resp = await _http.SendAsync(req, ct);
            return resp.IsSuccessStatusCode;
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException)
        {
            _log.LogDebug("pty output post for {Id} seq {Seq} failed: {Msg}", sessionId, seq, e.Message);
            return false;
        }
    }

    /// <summary>Tell the hub the session is over, so the console can stop polling and say
    /// why. Best-effort: if it doesn't land, the hub's idle reaper closes it anyway.</summary>
    public async Task ReportPtyClosedAsync(string sessionId, string reason, CancellationToken ct)
    {
        if (!_identity.IsEnrolled) return;
        var body = new JsonObject { ["reason"] = reason };
        try
        {
            using var req = Authorized(HttpMethod.Post, AgentConfig.PtyClosedUrl(sessionId));
            req.Content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json");
            using var resp = await _http.SendAsync(req, ct);
            _ = resp.IsSuccessStatusCode;
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException)
        {
            _log.LogDebug("pty close report for {Id} failed: {Msg}", sessionId, e.Message);
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

    /// <summary>
    /// Fetch a package payload and verify it before anyone is allowed to execute it.
    ///
    /// The hash is checked while the bytes are written, against the digest the HUB
    /// computed at upload time and shipped in the command params. That check is the whole
    /// integrity story for a hub-hosted payload — there is no signature here, deliberately
    /// (see fleet.py's docstring on why command signing was removed), so the trust root is
    /// the authenticated channel plus a hash the hub derived from the bytes it holds.
    ///
    /// A mismatch DELETES the file. Leaving a rejected installer on disk with a
    /// predictable name is how a failed verification turns into something that gets run
    /// anyway by a retry or a stray hand.
    ///
    /// `expectedSha256` may be null for url/unc payloads the operator chose not to pin;
    /// the download then succeeds unverified, which is the operator's stated intent.
    /// It is never null for a hub-hosted payload — the hub always has the digest.
    /// </summary>
    public async Task<string?> DownloadPackageAsync(
        string url, string destPath, string? expectedSha256, CancellationToken ct)
    {
        // A relative URL from the hub is resolved against the base this agent already
        // trusts, rather than anything in the payload — the params must not be able to
        // redirect the fetch to another host.
        if (!url.StartsWith("http://", StringComparison.OrdinalIgnoreCase)
            && !url.StartsWith("https://", StringComparison.OrdinalIgnoreCase))
        {
            url = AgentConfig.HubBase + (url.StartsWith('/') ? url : "/" + url);
        }

        try
        {
            using var req = new HttpRequestMessage(HttpMethod.Get, url);
            // Only our own hub gets the agent's bearer token. A package pulled from an
            // arbitrary operator-supplied URL must not have it attached — compared on the
            // parsed origin, because a string prefix test also matches
            // `https://<hub>.attacker.net` and `https://<hub>@attacker.net`. See
            // AgentConfig.IsHubUrl.
            if (AgentConfig.IsHubUrl(url))
                req.Headers.Authorization =
                    new AuthenticationHeaderValue("Bearer", _identity.BearerValue);

            using var resp = await _downloadHttp.SendAsync(
                req, HttpCompletionOption.ResponseHeadersRead, ct);
            if (!resp.IsSuccessStatusCode)
                return $"download failed: HTTP {(int)resp.StatusCode}";

            Directory.CreateDirectory(Path.GetDirectoryName(destPath)!);
            using (var source = await resp.Content.ReadAsStreamAsync(ct))
            using (var dest = File.Create(destPath))
            {
                await source.CopyToAsync(dest, ct);
            }

            if (!string.IsNullOrEmpty(expectedSha256))
            {
                string actual;
                using (var stream = File.OpenRead(destPath))
                    actual = Convert.ToHexString(await System.Security.Cryptography.SHA256
                        .HashDataAsync(stream, ct)).ToLowerInvariant();
                if (!string.Equals(actual, expectedSha256.Trim().ToLowerInvariant(),
                                   StringComparison.Ordinal))
                {
                    TryDelete(destPath);
                    return $"sha256 mismatch (got {actual}, expected {expectedSha256})";
                }
            }
            return null;
        }
        catch (Exception e)
        {
            TryDelete(destPath);
            return $"download failed: {e.Message}";
        }
    }

    private static void TryDelete(string path)
    {
        try { if (File.Exists(path)) File.Delete(path); } catch { /* best effort */ }
    }

    private HttpRequestMessage Authorized(HttpMethod method, string url)
    {
        var req = new HttpRequestMessage(method, url);
        req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", _identity.BearerValue);
        return req;
    }

    // ---------------------------------------------------------------- backups (#1b)

    /// <summary>
    /// PUT a finished backup archive. Returns null on success, or a reason.
    ///
    /// Two shapes, because the two destination kinds differ. An S3 destination gives a
    /// PRE-SIGNED url that carries its own signature in the query string — it must be sent
    /// WITHOUT our bearer header, since an extra Authorization header is not part of what
    /// was signed and S3 rejects the request. A WebDAV destination has no pre-signed
    /// concept, so the hub proxies: that URL is on the hub and does need the bearer.
    ///
    /// Streamed from disk with the long-timeout client — these are gigabytes on a link
    /// that may be a home DSL line, and the 10-second client would abort instantly.
    /// </summary>
    public async Task<string?> UploadBackupAsync(string url, string archivePath,
                                                 bool viaHub, CancellationToken ct)
    {
        try
        {
            var length = new FileInfo(archivePath).Length;
            using var body = new FileStream(archivePath, FileMode.Open, FileAccess.Read,
                                            FileShare.Read, 1024 * 1024, useAsync: true);
            using var req = viaHub
                ? Authorized(HttpMethod.Put, url)
                : new HttpRequestMessage(HttpMethod.Put, url);
            req.Content = new StreamContent(body);
            req.Content.Headers.ContentLength = length;
            req.Content.Headers.ContentType =
                new MediaTypeHeaderValue("application/octet-stream");

            using var resp = await _downloadHttp.SendAsync(req, ct);
            if (resp.IsSuccessStatusCode) return null;
            var text = await resp.Content.ReadAsStringAsync(ct);
            return $"Upload failed: HTTP {(int)resp.StatusCode} {text}"
                .Trim()[..Math.Min(300, $"Upload failed: HTTP {(int)resp.StatusCode} {text}".Trim().Length)];
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException or IOException)
        {
            return $"Upload failed: {e.Message}";
        }
    }

    /// <summary>
    /// Report a backup run's outcome and manifest. Returns true if the hub accepted it.
    ///
    /// Retried a few times: the archive is already uploaded at this point, and losing the
    /// manifest to one dropped connection would leave the hub believing the run never
    /// finished while the bytes sit in the bucket unreferenced.
    /// </summary>
    public async Task<bool> ReportBackupAsync(string runId, JsonNode payload,
                                              CancellationToken ct)
    {
        var url = $"{AgentConfig.HubBase}/api/agent/backups/{Uri.EscapeDataString(runId)}/result";
        for (int attempt = 0; attempt < 3; attempt++)
        {
            try
            {
                using var req = Authorized(HttpMethod.Post, url);
                req.Content = new StringContent(payload.ToJsonString(), Encoding.UTF8,
                                                "application/json");
                using var resp = await _downloadHttp.SendAsync(req, ct);
                if (resp.IsSuccessStatusCode) return true;
                // A 4xx is the hub refusing this payload; retrying will not change its mind.
                if ((int)resp.StatusCode is >= 400 and < 500)
                {
                    _log.LogWarning("Hub refused backup result ({Status})", (int)resp.StatusCode);
                    return false;
                }
            }
            catch (Exception e) when (e is HttpRequestException or TaskCanceledException)
            {
                _log.LogDebug("Backup result POST failed: {Msg}", e.Message);
            }
            if (attempt < 2) await Task.Delay(TimeSpan.FromSeconds(5 * (attempt + 1)), ct);
        }
        return false;
    }

    /// <summary>
    /// Fetch a restore's plan: which archives, which files inside them, and the key.
    ///
    /// A separate request rather than command params, and deliberately so — the hub audits
    /// command params verbatim, and a plan is tens of thousands of file names plus a
    /// decryption key. Returns null if the hub will not hand it over (a finished restore,
    /// or one belonging to another machine), which the caller reports as a failed run.
    /// </summary>
    public async Task<JsonObject?> FetchRestorePlanAsync(string restoreId, CancellationToken ct)
    {
        var url = $"{AgentConfig.HubBase}/api/agent/backups/restore/"
                  + $"{Uri.EscapeDataString(restoreId)}/plan";
        try
        {
            using var req = Authorized(HttpMethod.Get, url);
            using var resp = await _downloadHttp.SendAsync(req, ct);
            if (!resp.IsSuccessStatusCode)
            {
                _log.LogWarning("Hub refused the restore plan ({Status})", (int)resp.StatusCode);
                return null;
            }
            return JsonNode.Parse(await resp.Content.ReadAsStringAsync(ct))?.AsObject();
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException or JsonException)
        {
            _log.LogWarning("Could not fetch the restore plan: {Msg}", e.Message);
            return null;
        }
    }

    // ---------------------------------------------------------------- firmware (#9)

    /// <summary>
    /// Fetch the attribute list and BIOS setup password for one firmware change.
    ///
    /// Fetched rather than carried in the command's params for the same reason the restore
    /// plan is: the hub audits params verbatim, so a setup password there would be written
    /// into the audit log inside the database that is itself backed up.
    ///
    /// A refusal is a legitimate outcome, not a transport failure — the hub flips the change
    /// to running on the first successful fetch, so a redelivered command is turned away
    /// rather than replaying writes against firmware. Null covers both, and the executor
    /// reports it as "the hub would not supply this change"; retrying would be exactly wrong.
    /// </summary>
    public async Task<JsonObject?> FetchBiosChangeAsync(string changeId, CancellationToken ct)
    {
        var url = $"{AgentConfig.HubBase}/api/agent/bios/change/{Uri.EscapeDataString(changeId)}";
        try
        {
            using var req = Authorized(HttpMethod.Get, url);
            using var resp = await _downloadHttp.SendAsync(req, ct);
            if (!resp.IsSuccessStatusCode)
            {
                _log.LogWarning("Hub refused the BIOS change payload ({Status})",
                                (int)resp.StatusCode);
                return null;
            }
            return JsonNode.Parse(await resp.Content.ReadAsStringAsync(ct))?.AsObject();
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException or JsonException)
        {
            _log.LogWarning("Could not fetch the BIOS change payload: {Msg}", e.Message);
            return null;
        }
    }

    /// <summary>
    /// Fetch one firmware update's payload: the image URL and digest, the vendor and model
    /// list to re-check against, the BIOS setup password and the power policy (roadmap #9).
    ///
    /// Null on any refusal, and the caller must then do NOTHING -- the hub answers 409 for an
    /// update that is no longer pending, which is exactly how a redelivered command is stopped
    /// from flashing a machine twice.
    /// </summary>
    public async Task<JsonObject?> FetchFirmwareUpdateAsync(string updateId, CancellationToken ct)
    {
        var url = $"{AgentConfig.HubBase}/api/agent/firmware/update/"
                  + $"{Uri.EscapeDataString(updateId)}";
        try
        {
            using var req = Authorized(HttpMethod.Get, url);
            using var resp = await _downloadHttp.SendAsync(req, ct);
            if (!resp.IsSuccessStatusCode)
            {
                _log.LogWarning("Hub refused the firmware update payload ({Status})",
                                (int)resp.StatusCode);
                return null;
            }
            return JsonNode.Parse(await resp.Content.ReadAsStringAsync(ct))?.AsObject();
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException or JsonException)
        {
            _log.LogWarning("Could not fetch the firmware update payload: {Msg}", e.Message);
            return null;
        }
    }

    /// <summary>
    /// Report what the manufacturer's updater did.
    ///
    /// Retried harder than most reports, because by the time this runs the image may already
    /// be staged in the firmware: losing the report to one dropped connection would leave the
    /// console showing an update that never left the hub over a machine that is about to flash
    /// on its next restart. The machine's own BIOS-version report can still confirm it, but
    /// only this call can say the attempt was made at all.
    /// </summary>
    public async Task<bool> ReportFirmwareUpdateAsync(string updateId, JsonNode payload,
                                                      CancellationToken ct)
    {
        var url = $"{AgentConfig.HubBase}/api/agent/firmware/update/"
                  + $"{Uri.EscapeDataString(updateId)}/result";
        for (int attempt = 0; attempt < 3; attempt++)
        {
            try
            {
                using var req = Authorized(HttpMethod.Post, url);
                req.Content = new StringContent(payload.ToJsonString(), Encoding.UTF8,
                                                "application/json");
                using var resp = await _downloadHttp.SendAsync(req, ct);
                if (resp.IsSuccessStatusCode) return true;
                if ((int)resp.StatusCode is >= 400 and < 500)
                {
                    _log.LogWarning("Hub refused the firmware update result ({Status})",
                                    (int)resp.StatusCode);
                    return false;
                }
            }
            catch (Exception e) when (e is HttpRequestException or TaskCanceledException)
            {
                _log.LogDebug("Firmware update result POST failed: {Msg}", e.Message);
            }
            if (attempt < 2) await Task.Delay(TimeSpan.FromSeconds(5 * (attempt + 1)), ct);
        }
        return false;
    }

    /// <summary>
    /// Report what the firmware said when the settings were read back.
    ///
    /// Retried like the backup and restore reports, and here the stakes are the same: the
    /// firmware is already written by this point, so losing the report to one dropped
    /// connection would leave the console showing a change in flight over a machine that
    /// finished minutes ago — and the hub cannot re-derive it, since only this machine saw the
    /// values before and after.
    /// </summary>
    public async Task<bool> ReportBiosChangeAsync(string changeId, JsonNode payload,
                                                  CancellationToken ct)
    {
        var url = $"{AgentConfig.HubBase}/api/agent/bios/change/"
                  + $"{Uri.EscapeDataString(changeId)}/result";
        for (int attempt = 0; attempt < 3; attempt++)
        {
            try
            {
                using var req = Authorized(HttpMethod.Post, url);
                req.Content = new StringContent(payload.ToJsonString(), Encoding.UTF8,
                                                "application/json");
                using var resp = await _downloadHttp.SendAsync(req, ct);
                if (resp.IsSuccessStatusCode) return true;
                if ((int)resp.StatusCode is >= 400 and < 500)
                {
                    _log.LogWarning("Hub refused the BIOS change result ({Status})",
                                    (int)resp.StatusCode);
                    return false;
                }
            }
            catch (Exception e) when (e is HttpRequestException or TaskCanceledException)
            {
                _log.LogDebug("BIOS change result POST failed: {Msg}", e.Message);
            }
            if (attempt < 2) await Task.Delay(TimeSpan.FromSeconds(5 * (attempt + 1)), ct);
        }
        return false;
    }

    /// <summary>
    /// Download one backup archive to <paramref name="targetPath"/>. Returns null on
    /// success, or a reason.
    ///
    /// The mirror of UploadBackupAsync, with the same split: a pre-signed S3 URL carries
    /// its own signature and must NOT be sent our bearer header, while a hub-proxied
    /// WebDAV download is on the hub and does need it.
    /// </summary>
    public async Task<string?> DownloadBackupAsync(string url, string targetPath,
                                                   bool viaHub, CancellationToken ct)
    {
        try
        {
            using var req = viaHub
                ? Authorized(HttpMethod.Get, url)
                : new HttpRequestMessage(HttpMethod.Get, url);
            using var resp = await _downloadHttp.SendAsync(
                req, HttpCompletionOption.ResponseHeadersRead, ct);
            if (!resp.IsSuccessStatusCode)
            {
                var text = await resp.Content.ReadAsStringAsync(ct);
                var message = $"Download failed: HTTP {(int)resp.StatusCode} {text}".Trim();
                return message[..Math.Min(300, message.Length)];
            }
            Directory.CreateDirectory(Path.GetDirectoryName(targetPath)!);
            await using (var file = new FileStream(targetPath, FileMode.Create, FileAccess.Write,
                                                   FileShare.None, 1024 * 1024, useAsync: true))
            {
                await resp.Content.CopyToAsync(file, ct);
            }
            return null;
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException or IOException)
        {
            return $"Download failed: {e.Message}";
        }
    }

    /// <summary>
    /// Report a restore's outcome. Retried like ReportBackupAsync and for the same reason:
    /// the files are already written by this point, and losing the report to one dropped
    /// connection leaves an operator watching a restore that finished an hour ago.
    /// </summary>
    public async Task<bool> ReportRestoreAsync(string restoreId, JsonNode payload,
                                               CancellationToken ct)
    {
        var url = $"{AgentConfig.HubBase}/api/agent/backups/restore/"
                  + $"{Uri.EscapeDataString(restoreId)}/result";
        for (int attempt = 0; attempt < 3; attempt++)
        {
            try
            {
                using var req = Authorized(HttpMethod.Post, url);
                req.Content = new StringContent(payload.ToJsonString(), Encoding.UTF8,
                                                "application/json");
                using var resp = await _downloadHttp.SendAsync(req, ct);
                if (resp.IsSuccessStatusCode) return true;
                if ((int)resp.StatusCode is >= 400 and < 500)
                {
                    _log.LogWarning("Hub refused restore result ({Status})", (int)resp.StatusCode);
                    return false;
                }
            }
            catch (Exception e) when (e is HttpRequestException or TaskCanceledException)
            {
                _log.LogDebug("Restore result POST failed: {Msg}", e.Message);
            }
            if (attempt < 2) await Task.Delay(TimeSpan.FromSeconds(5 * (attempt + 1)), ct);
        }
        return false;
    }

    private sealed class CommandsResponse
    {
        [System.Text.Json.Serialization.JsonPropertyName("commands")]
        public List<FleetCommand>? Commands { get; set; }

        [System.Text.Json.Serialization.JsonPropertyName("waited")]
        public bool Waited { get; set; }
    }

    public void Dispose()
    {
        _http.Dispose();
        _downloadHttp.Dispose();
        _commandHttp.Dispose();
    }
}
