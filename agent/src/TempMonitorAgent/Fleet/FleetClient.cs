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
public sealed class FleetClient : IDisposable, IOutputSink, IPackageDownloader
{
    private readonly ILogger<FleetClient> _log;
    private readonly AgentState _state;
    private readonly HttpClient _http;
    // Package payloads are hundreds of megabytes on a slow link, so they cannot share
    // the 10-second client above — that timeout is a *whole request* budget, not an idle
    // one, and would abort every large installer mid-download.
    private readonly HttpClient _downloadHttp;
    private AgentIdentity _identity;

    public FleetClient(ILogger<FleetClient> log, AgentState state)
    {
        _log = log;
        _state = state;
        _identity = state.LoadIdentity();
        _http = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
        _downloadHttp = new HttpClient { Timeout = TimeSpan.FromMinutes(30) };
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
            req.Content = new StringContent(body.ToJsonString(), Encoding.UTF8,
                                            "application/json");

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
            // arbitrary operator-supplied URL must not have it attached.
            if (url.StartsWith(AgentConfig.HubBase, StringComparison.OrdinalIgnoreCase))
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
    }

    public void Dispose()
    {
        _http.Dispose();
        _downloadHttp.Dispose();
    }
}
