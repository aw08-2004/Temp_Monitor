using System.Net;
using System.Text;
using System.Text.Json;
using Microsoft.Extensions.Logging;

namespace TempMonitorAgent.Telemetry;

public readonly record struct ReportResult(bool Sent, int? StatusCode, string? LatestVersion);

/// <summary>
/// Builds the telemetry payload and POSTs it to the hub's open /api/report endpoint
/// (no auth, 3s timeout, no redirects), with an offline buffer that mirrors
/// companion.py: on a connectivity failure a sensor-stripped copy is queued (bounded
/// at 1000, oldest dropped) and flushed oldest-first on the next success — stopping on
/// a connectivity error or HTTP >= 500, dropping an unretryable 4xx.
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
            return new ReportResult(false, null, null);
        }

        using (resp)
        {
            var status = (int)resp.StatusCode;
            string? latest = null;
            var body = await SafeReadBody(resp, ct);
            if (resp.IsSuccessStatusCode)
            {
                latest = TryParseLatestVersion(body);
                await FlushOfflineAsync(ct);
            }
            _log.LogInformation("Sent: {Temp}°C - Hub responded: {Status}", temp, status);
            return new ReportResult(true, status, latest);
        }
    }

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
            ["asset_tag"] = _identity.AssetTag,
            ["service_tag"] = _identity.ServiceTag,
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
        // Keep temp history light — never hoard ~36 KB sensor blobs.
        var stripped = new Dictionary<string, object?>(payload);
        stripped.Remove("sensors");
        while (_offline.Count >= AgentConfig.OfflineBufferMax) _offline.Dequeue();
        _offline.Enqueue(stripped);
    }

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
                return; // still offline — keep the rest for later
            }

            using (resp)
            {
                if (resp.IsSuccessStatusCode) { _offline.Dequeue(); continue; }
                if ((int)resp.StatusCode >= 500) return;       // server hiccup — retry later
                _offline.Dequeue();                            // 4xx — unretryable, drop it
            }
        }
    }

    private static async Task<string?> SafeReadBody(HttpResponseMessage resp, CancellationToken ct)
    {
        try { return await resp.Content.ReadAsStringAsync(ct); }
        catch { return null; }
    }

    private static string? TryParseLatestVersion(string? body)
    {
        if (string.IsNullOrEmpty(body)) return null;
        try
        {
            using var doc = JsonDocument.Parse(body);
            if (doc.RootElement.ValueKind == JsonValueKind.Object &&
                doc.RootElement.TryGetProperty("latest_version", out var v) &&
                v.ValueKind == JsonValueKind.String)
            {
                return v.GetString();
            }
        }
        catch { /* ignore */ }
        return null;
    }

    public void Dispose() => _http.Dispose();
}
