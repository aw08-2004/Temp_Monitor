using System.Text.Json.Nodes;

namespace FleetHubAgent.Telemetry;

/// <summary>
/// Samples the process list only while somebody has the machine's Processes card open, and
/// hands the latest sample to a heartbeat (roadmap #22). The Windows agent's ProcessReporter,
/// ported: same demand gate, same wire shape, same "one pending sample, newest wins".
///
/// <para><b>Demand, not cadence, is what bounds it.</b> A process list has changed by
/// definition, so change-only would mean every sample; what keeps an idle fleet quiet is that
/// nothing is sampled at all until the hub says <c>wanted</c>, and the pending sample is
/// dropped the moment it says otherwise.</para>
///
/// <para>An instance rather than the Windows agent's static class, matching how this agent
/// holds <c>PatchInventoryReporter</c>: the reader carries a baseline between samples, and
/// the container owning it keeps that state in one obvious place.</para>
/// </summary>
public sealed class ProcessReporter
{
    /// <summary>How long the baseline is allowed to accumulate before the first real sample.
    /// Long enough that a process doing something shows up, short enough that the operator
    /// who just opened the card is not left waiting.</summary>
    private const int BaselineWindowMillis = 1000;

    private readonly ProcessReader _reader;
    private readonly Lock _gate = new();
    private bool _wanted;
    private JsonObject? _pending;

    public ProcessReporter(ProcessReader? reader = null) => _reader = reader ?? new ProcessReader();

    public bool Wanted
    {
        get { lock (_gate) return _wanted; }
    }

    public void SetWanted(bool wanted)
    {
        lock (_gate)
        {
            if (_wanted && !wanted)
            {
                _pending = null;
                _reader.Reset();
            }
            _wanted = wanted;
        }
    }

    /// <summary>Take one sample if anybody is watching. True when a payload is now waiting.</summary>
    public async Task<bool> SampleAsync(CancellationToken ct)
    {
        if (!Wanted) return false;
        var snapshot = _reader.Sample();
        if (snapshot is null)
        {
            try { await Task.Delay(BaselineWindowMillis, ct); }
            catch (OperationCanceledException) { return false; }
            if (!Wanted) return false;
            snapshot = _reader.Sample();
            if (snapshot is null) return false;
        }
        var payload = ToPayload(snapshot);
        lock (_gate)
        {
            if (!_wanted) return false;   // the watch lapsed while we were sampling
            _pending = payload;
            return true;
        }
    }

    /// <summary>The newest sample for a heartbeat, or null. Taken, not peeked: a process list
    /// that failed to send is stale by the next sample anyway, so there is nothing to retry.</summary>
    public JsonObject? TakeLatest()
    {
        lock (_gate)
        {
            var payload = _pending;
            _pending = null;
            return payload;
        }
    }

    /// <summary>The wire shape, field for field the Windows agent's -- hub/processes.py
    /// reads one shape, and a Linux list that differed would be a card that half-renders.
    /// <c>session</c> is left out: it is a Windows logon-session id with no Linux
    /// equivalent a helpdesk would recognise, and the hub stores its absence as null.</summary>
    public static JsonObject ToPayload(ProcessSnapshot snapshot)
    {
        var list = new JsonArray();
        foreach (var entry in snapshot.Processes)
        {
            var item = new JsonObject
            {
                ["pid"] = entry.Pid,
                ["name"] = entry.Name,
                ["cpu_pct"] = Math.Round(entry.CpuPercent, 2),
                ["mem_mb"] = Math.Round(entry.MemoryMb, 1),
                ["user"] = entry.User,
                ["path"] = entry.Path,
            };
            item["started_at"] = entry.StartedAt is null ? null : JsonValue.Create(entry.StartedAt.Value);
            if (entry.Services.Count > 0)
            {
                var services = new JsonArray();
                foreach (var s in entry.Services) services.Add(s);
                item["services"] = services;
            }
            list.Add(item);
        }
        return new JsonObject
        {
            ["captured_at"] = DateTimeOffset.UtcNow.ToUnixTimeSeconds(),
            ["cpu_cores"] = snapshot.CpuCores,
            ["mem_total_mb"] = Math.Round(snapshot.MemoryTotalMb, 1),
            ["sample_ms"] = snapshot.SampleMillis,
            ["truncated"] = snapshot.Truncated,
            ["processes"] = list,
        };
    }
}
