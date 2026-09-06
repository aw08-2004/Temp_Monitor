using System.Text;
using System.Text.Json;
using Microsoft.Extensions.Logging;

namespace FleetHubAgent.Telemetry;

public readonly record struct ReportResult(bool Sent, int? StatusCode);

/// <summary>
/// Builds the telemetry payload and POSTs it to the hub's open /api/report endpoint (no auth,
/// 3s timeout, no redirects), with an offline buffer.
///
/// The buffer is the reason this class is not four lines. A machine that loses its link keeps
/// reading its own sensors; on a connectivity failure a sensor-stripped copy of the payload is
/// queued (bounded at 1000, oldest dropped) and flushed oldest-first on the next success, so a
/// reconnect backfills the gap instead of leaving a hole in the history. Each queued copy
/// carries the client_ts it was BUILT with -- the hub backdates a reading to that timestamp
/// (within data.ingest_max_backdate_days), which is what makes the backfill land where it
/// belongs on the chart rather than all at once at reconnect.
///
/// **Sensors are stripped from a buffered copy on purpose.** A full sensor block is tens of
/// kilobytes; a thousand of them is a machine holding tens of megabytes of history nobody
/// asked for, on a box that may be offline precisely because something is wrong with it. The
/// temperature is what the history needs.
///
/// This posts to the ONE endpoint that is deliberately unauthenticated, which is why it is a
/// separate client from FleetClient and why it carries no bearer token: /api/report predates
/// enrollment and still serves machines that have never enrolled. This agent reports there
/// before it has an identity, and that is what makes a Linux box visible in the console within
/// five seconds of starting even when its enrollment secret is missing.
/// </summary>
public sealed class TelemetryReporter : IDisposable
{
    private readonly ILogger<TelemetryReporter> _log;
    private readonly HttpClient _http;
    private readonly SystemIdentity _identity;
    private readonly Queue<Dictionary<string, object?>> _offline = new();

    public TelemetryReporter(ILogger<TelemetryReporter> log, SystemIdentity identity)
    {
        _log = log;
        _identity = identity;
        // No redirects: this request carries machine identity, and a hub that has been
        // redirected somewhere else is a hub we should notice rather than follow.
        _http = new HttpClient(new HttpClientHandler { AllowAutoRedirect = false })
        {
            Timeout = TimeSpan.FromSeconds(3),
        };
    }

    public async Task<ReportResult> ReportAsync(
        double temp, IReadOnlyList<SensorReading>? sensors, long? uptime, CancellationToken ct)
    {
        var payload = BuildPayload(temp, sensors, uptime);
        HttpResponseMessage resp;
        try
        {
            resp = await PostAsync(payload, ct);
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException)
        {
            EnqueueStripped(payload);
            _log.LogWarning("Report failed ({Msg}); buffered ({Count} queued)", e.Message, _offline.Count);
            return new ReportResult(false, null);
        }

        using (resp)
        {
            var status = (int)resp.StatusCode;
            if (resp.IsSuccessStatusCode) await FlushOfflineAsync(ct);
            _log.LogInformation("Sent: {Temp} °C - hub responded {Status}", temp, status);
            return new ReportResult(true, status);
        }
    }

    /// <summary>
    /// The report body, in the hub's field names.
    ///
    /// Note what is NOT read back: /api/report answers with `latest_version` when the reporter
    /// is on the agent release train, and the Windows agent uses it to check for an update
    /// early. This agent reports a 0.x version, which is below the hub's
    /// AGENT_TRAIN_MIN_VERSION, so the hub deliberately sends nothing -- and there is no
    /// self-updater here to act on it if it did. See AgentConfig.Version.
    /// </summary>
    private Dictionary<string, object?> BuildPayload(
        double temp, IReadOnlyList<SensorReading>? sensors, long? uptime)
    {
        var payload = new Dictionary<string, object?>
        {
            ["machine"] = AgentConfig.MachineName,
            ["temp"] = temp,
            ["companion_version"] = AgentConfig.Version,
            ["client_ts"] = DateTimeOffset.UtcNow.ToUnixTimeSeconds(),
            ["serial_number"] = _identity.SerialNumber,
            ["model"] = _identity.Model,
            ["manufacturer"] = _identity.Manufacturer,
            ["asset_tag"] = _identity.AssetTag,
            // On every report rather than on the heartbeat: the hub COALESCEs a null away, so
            // a report that could not read the OS costs nothing and one that could fills the
            // gap at the next tick.
            ["os_caption"] = _identity.OsCaption,
            ["os_version"] = _identity.OsVersion,
            ["os_build"] = _identity.OsBuild,
            ["os_arch"] = _identity.OsArchitecture,
        };
        if (sensors is not null) payload["sensors"] = sensors;
        if (uptime is not null) payload["uptime_seconds"] = uptime;
        return payload;
    }

    private async Task<HttpResponseMessage> PostAsync(
        Dictionary<string, object?> payload, CancellationToken ct)
    {
        var json = JsonSerializer.Serialize(payload);
        using var content = new StringContent(json, Encoding.UTF8, "application/json");
        return await _http.PostAsync(AgentConfig.ReportUrl, content, ct);
    }

    private void EnqueueStripped(Dictionary<string, object?> payload)
    {
        var stripped = new Dictionary<string, object?>(payload);
        stripped.Remove("sensors");
        while (_offline.Count >= AgentConfig.OfflineBufferMax) _offline.Dequeue();
        _offline.Enqueue(stripped);
    }

    /// <summary>Drain the buffer oldest-first, stopping on anything that might succeed later
    /// and dropping anything that will not.
    ///
    /// The three outcomes are deliberately different. A connectivity error means we are
    /// offline again -- keep everything. A 5xx means the hub is unwell -- keep everything, it
    /// will be up later. A 4xx means the hub has looked at this payload and refused it, and
    /// retrying it forever would wedge the queue behind one bad item and lose every good
    /// reading behind it.</summary>
    private async Task FlushOfflineAsync(CancellationToken ct)
    {
        while (_offline.Count > 0)
        {
            var item = _offline.Peek();
            HttpResponseMessage resp;
            try
            {
                resp = await PostAsync(item, ct);
            }
            catch (Exception e) when (e is HttpRequestException or TaskCanceledException)
            {
                return; // still offline -- keep the rest for later
            }

            using (resp)
            {
                if (resp.IsSuccessStatusCode) { _offline.Dequeue(); continue; }
                if ((int)resp.StatusCode >= 500) return;       // server hiccup -- retry later
                _offline.Dequeue();                            // 4xx -- unretryable, drop it
            }
        }
    }

    public void Dispose() => _http.Dispose();
}
