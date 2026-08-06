using System.Text.Json.Nodes;

namespace TempMonitorAgent.Telemetry;

/// <summary>
/// Carries this machine's process list to the hub on the heartbeat, but ONLY while an
/// operator is looking at it.
///
/// **This is the one heartbeat payload that is not change-only, and the one that is not
/// always on.** Its siblings (<c>BackupProfileReporter</c>, <c>RemoteInventoryReporter</c>,
/// <c>BiosInventoryReporter</c>, <c>NetworkInventoryReporter</c>) all answer "has this
/// machine's inventory changed?", and the answer is usually no, so they send nothing. A
/// process list has changed by definition -- it changes between two reads of the same
/// second -- so change detection would buy nothing and every report would be sent anyway.
/// What bounds the cost instead is DEMAND: the hub sets <c>processes_wanted</c> on the
/// heartbeat reply while somebody has that machine's Processes card open, and clears it
/// seconds after they leave. A machine nobody is looking at samples nothing and sends
/// nothing, which is the only reason a ~60 KB payload on a five-second cadence is
/// affordable at all.
///
/// **The first sample after being asked produces nothing to send**, because CPU is a rate
/// and one look cannot measure one (see ProcessReader). <see cref="SampleAsync"/> therefore
/// takes the baseline, waits a beat, and samples again -- so the operator waits about a
/// second rather than a whole extra cadence for their first list.
/// </summary>
public static class ProcessReporter
{
    /// <summary>How long to wait between the baseline and the sample that follows it on the
    /// first pass. Long enough that a process using a whole core registers meaningfully
    /// (10ms of CPU time in the window at 15ms timer granularity is noise), short enough
    /// that it does not show up as latency on the operator's first paint.</summary>
    private const int BaselineWindowMillis = 1000;

    private static readonly Lock Gate = new();
    private static bool _wanted;
    private static JsonObject? _pending;

    /// <summary>Is the hub asking for process reports? Set from the heartbeat reply.</summary>
    public static bool Wanted
    {
        get { lock (Gate) return _wanted; }
    }

    /// <summary>Apply the hub's answer. Turning the watch OFF drops the CPU baseline and the
    /// metadata caches: a card reopened an hour later must measure a fresh window, not
    /// average every process's CPU across the whole gap.</summary>
    public static void SetWanted(bool wanted)
    {
        bool changed;
        lock (Gate)
        {
            changed = _wanted != wanted;
            _wanted = wanted;
            if (!wanted) _pending = null;
        }
        if (changed && !wanted) ProcessReader.Reset();
    }

    /// <summary>True when a sample is waiting for a heartbeat to carry it.</summary>
    public static bool HasPending
    {
        get { lock (Gate) return _pending is not null; }
    }

    /// <summary>
    /// Take a sample, if one is wanted, and leave it for the next heartbeat.
    ///
    /// Returns true when a payload is now pending, so the caller can send a heartbeat
    /// immediately instead of leaving a fresh list sitting here for up to the heartbeat
    /// interval -- an operator watching this card is watching it now.
    /// </summary>
    public static async Task<bool> SampleAsync(CancellationToken ct)
    {
        if (!Wanted) return false;

        var snapshot = ProcessReader.Sample();
        if (snapshot is null)
        {
            // First pass since the watch was turned on: that call established the baseline.
            // Wait a short window and take the real sample rather than making the operator
            // wait out another whole cadence for a list with a CPU column.
            try { await Task.Delay(BaselineWindowMillis, ct); }
            catch (OperationCanceledException) { return false; }
            if (!Wanted) return false;
            snapshot = ProcessReader.Sample();
            if (snapshot is null) return false;
        }

        var payload = ToPayload(snapshot);
        lock (Gate)
        {
            if (!_wanted) return false;    // the watch lapsed while we were sampling
            _pending = payload;
            return true;
        }
    }

    /// <summary>Hand the pending sample to a heartbeat, or null when there is none.
    ///
    /// Deliberately NOT the <c>TakeIfChanged</c> its siblings expose: there is no "changed"
    /// question to ask, and a failed heartbeat simply drops this sample rather than queuing
    /// it -- a process list that did not make it is worth exactly nothing five seconds
    /// later, when the next one is already being taken.</summary>
    public static JsonObject? TakeLatest()
    {
        lock (Gate)
        {
            var payload = _pending;
            _pending = null;
            return payload;
        }
    }

    /// <summary>The wire shape. Separate and pure so a test can assert on exactly what the
    /// hub will receive without a process anywhere near it -- the two halves of this feature
    /// are a C# reader and a Python ingest, and this object is all that binds them.</summary>
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
                ["session"] = entry.Session,
                ["path"] = entry.Path,
            };
            // Null rather than 0: the hub stores "unknown" distinctly, and a start time of
            // 1970 would be rendered as a fact.
            item["started_at"] = entry.StartedAt is null
                ? null
                : JsonValue.Create(entry.StartedAt.Value);
            if (entry.Services.Count > 0)
            {
                var services = new JsonArray();
                foreach (var name in entry.Services) services.Add(name);
                item["services"] = services;
            }
            list.Add(item);
        }

        return new JsonObject
        {
            ["captured_at"] = DateTimeOffset.UtcNow.ToUnixTimeSeconds(),
            ["cpu_cores"] = snapshot.CpuCores,
            ["mem_total_mb"] = snapshot.MemoryTotalMb,
            ["sample_ms"] = snapshot.SampleMillis,
            ["truncated"] = snapshot.Truncated,
            ["processes"] = list,
        };
    }
}
