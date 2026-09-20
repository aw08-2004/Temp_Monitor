using System.Text.Json.Nodes;

namespace TempMonitorAgent.Events;

/// <summary>
/// Carries this machine's matching event log records to the hub on the heartbeat
/// (roadmap #16), following <c>PatchInventoryReporter</c> and its siblings.
///
/// <para><b>Not change-only, and not demand-driven either -- it is the one reporter that is
/// neither.</b> Its siblings send nothing when their inventory has not changed, and
/// <c>ProcessReporter</c> sends nothing unless somebody is looking. This one sends a payload
/// on every pass while any subscription exists, INCLUDING an empty one, and that empty report
/// is the point: it is the only thing separating "your fleet is quiet" from "your collector
/// stopped", which is the failure every log console gets wrong. The hub stores it as a
/// liveness row (machine_event_state) and says so on the machine's page.</para>
///
/// <para><b>A machine with no subscriptions sends nothing at all.</b> Not an empty report --
/// nothing. A hub with the feature switched off must cost the fleet zero bytes and zero
/// event-log reads, which is the state every hub upgrades into, and an empty report every
/// minute from two hundred PCs is not zero.</para>
///
/// <para><b>Scanned off the heartbeat path</b>, like its siblings: an event log query is an
/// IPC round trip to the Windows event service which can take a second on a busy channel,
/// and the hub's offline window is 90 s. The scan runs on the agent's inventory loop and
/// merely leaves a payload behind.</para>
///
/// <para><b>De-duplication is by RecordId, per channel.</b> The query window is relative and
/// overlaps the previous one on purpose (see <c>WindowsEventReader</c>), so the same record
/// is routinely read twice; sending it twice would double-count a 4625 storm on the page
/// somebody is using to count one. The seen-set is bounded and dropped when the subscription
/// document changes.</para>
/// </summary>
public static class EventLogReporter
{
    /// <summary>How often the logs are queried. A minute rather than the heartbeat's ten
    /// seconds: nothing here is live -- the hub rolls repeats up over five minutes anyway --
    /// and six times fewer queries against the Windows event service is six times less of the
    /// only cost this feature has on the machine.</summary>
    private static readonly TimeSpan ScanInterval = TimeSpan.FromMinutes(1);

    /// <summary>How far back a query reaches, beyond the time since the last scan. Covers a
    /// scan that ran late (a busy machine, a service restart) without reaching so far back
    /// that a resumed agent re-reads an afternoon. Records read twice are dropped by
    /// RecordId, so the overlap costs a comparison rather than a duplicate.</summary>
    private static readonly TimeSpan Overlap = TimeSpan.FromSeconds(30);

    /// <summary>The longest window one query may ask for. An agent that was stopped for a
    /// week must not come back and ask the Security channel for a week of records: what it
    /// would deliver is a burst the hub then has to cap anyway, and what it costs is a scan
    /// that blocks the inventory loop. The gap is dropped, and that is the honest outcome --
    /// nothing was collecting while the service was down.</summary>
    private static readonly TimeSpan MaxWindow = TimeSpan.FromMinutes(15);

    /// <summary>RecordIds remembered per channel, for de-duplication across overlapping
    /// windows. Bounded: two windows' worth of a busy channel, after which the oldest are
    /// forgotten -- a record old enough to fall out of this set is older than any window
    /// still being queried, so it cannot come back.</summary>
    private const int MaxSeenPerChannel = 2000;

    private static readonly Lock Gate = new();
    private static DateTimeOffset _lastScan = DateTimeOffset.MinValue;
    private static string _appliedVersion = "";
    private static readonly Dictionary<string, Queue<long>> SeenOrder = new(StringComparer.OrdinalIgnoreCase);
    private static readonly Dictionary<string, HashSet<long>> Seen = new(StringComparer.OrdinalIgnoreCase);
    private static JsonObject? _pending;

    /// <summary>Re-scan if due. Cheap to call often; does nothing until the interval elapses,
    /// and nothing at all while no subscription exists.</summary>
    public static void RefreshIfDue()
    {
        var subscriptions = EventSubscriptionStore.Current;
        var version = EventSubscriptionStore.Version;

        TimeSpan sinceLast;
        lock (Gate)
        {
            // A changed document resets the de-duplication state and the clock. It has to:
            // the new subscriptions may name a channel this machine has never queried, and
            // carrying the old watermark into it would silently skip the first window of the
            // thing somebody just asked for.
            if (version != _appliedVersion)
            {
                _appliedVersion = version;
                _lastScan = DateTimeOffset.MinValue;
                Seen.Clear();
                SeenOrder.Clear();
                _pending = null;
            }
            if (subscriptions.Count == 0) return;
            var now = DateTimeOffset.UtcNow;
            if (_lastScan != DateTimeOffset.MinValue && now - _lastScan < ScanInterval) return;
            sinceLast = _lastScan == DateTimeOffset.MinValue ? ScanInterval : now - _lastScan;
            _lastScan = now;
        }

        var window = sinceLast + Overlap;
        if (window > MaxWindow) window = MaxWindow;

        var records = new List<WindowsEventReader.Record>();
        string? error = null;
        // One query per CHANNEL, not per subscription: three subscriptions on Security are
        // one XPath with three OR'd clauses, which is one round trip instead of three.
        foreach (var group in subscriptions.GroupBy(s => s.Log, StringComparer.OrdinalIgnoreCase))
        {
            try
            {
                records.AddRange(WindowsEventReader.Read(group.Key, group.ToList(),
                                                         (int)window.TotalMilliseconds));
            }
            catch (Exception e)
            {
                // Never fatal, and never silent. A channel that cannot be read (it does not
                // exist here, or LocalSystem is refused it) is a fact an operator needs, and
                // it reaches them as `error` on the report rather than only in a log file on
                // the machine they cannot see. One failing channel does not cost the others.
                error = $"{group.Key}: {e.Message}";
            }
        }

        var fresh = Deduplicate(records);
        fresh.Sort((left, right) => left.OccurredAt.CompareTo(right.OccurredAt));
        var cap = EventSubscriptionStore.MaxPerReport;
        var dropped = 0;
        if (fresh.Count > cap)
        {
            // The NEWEST are kept. A machine mid-storm produces more than a report can carry,
            // and the records somebody is about to look at are the recent ones; `dropped`
            // says how many went, so the console never has to guess whether it saw the whole
            // burst.
            dropped = fresh.Count - cap;
            fresh.RemoveRange(0, dropped);
        }

        var payload = ToPayload(fresh, dropped, error);
        lock (Gate) { _pending = payload; }
    }

    /// <summary>Hand the pending payload to a heartbeat, or null when there is nothing to
    /// send. Unlike its change-only siblings, "nothing to send" here means no scan has
    /// happened since the last heartbeat -- not that nothing was found.</summary>
    public static JsonObject? TakeIfReady()
    {
        lock (Gate)
        {
            var payload = _pending;
            _pending = null;
            return payload;
        }
    }

    /// <summary>Drop records already reported, and remember the rest. Bounded per channel.</summary>
    private static List<WindowsEventReader.Record> Deduplicate(
        List<WindowsEventReader.Record> records)
    {
        var fresh = new List<WindowsEventReader.Record>();
        lock (Gate)
        {
            foreach (var record in records)
            {
                // RecordId 0 means the channel did not supply one. Those cannot be
                // de-duplicated, so they are passed through: the hub's five-minute roll-up
                // collapses an identical repeat anyway, which is the weaker guarantee but
                // the right direction -- a record reported twice is visible and wrong by a
                // count, where a record dropped is invisible.
                if (record.RecordId == 0) { fresh.Add(record); continue; }
                if (!Seen.TryGetValue(record.Log, out var seen))
                {
                    seen = new HashSet<long>();
                    Seen[record.Log] = seen;
                    SeenOrder[record.Log] = new Queue<long>();
                }
                if (!seen.Add(record.RecordId)) continue;
                var order = SeenOrder[record.Log];
                order.Enqueue(record.RecordId);
                while (order.Count > MaxSeenPerChannel) seen.Remove(order.Dequeue());
                fresh.Add(record);
            }
        }
        return fresh;
    }

    /// <summary>The wire shape. Separate and pure so a test can assert on what the hub will
    /// receive without a Windows event service anywhere near it -- the two halves of this
    /// feature are a C# query and a Python ingest, and the only thing binding them is this
    /// object.
    ///
    /// <c>events</c> is always present, even when empty. See the class remarks: that is the
    /// whole reason the payload is an object rather than the array itself, and it is what
    /// the hub tests with <c>is not None</c>.</summary>
    public static JsonObject ToPayload(IReadOnlyList<WindowsEventReader.Record> records,
                                       int dropped, string? error)
    {
        var array = new JsonArray();
        foreach (var record in records)
        {
            array.Add(new JsonObject
            {
                ["log"] = record.Log,
                ["provider"] = record.Provider,
                ["event_id"] = record.EventId,
                ["level"] = record.Level,
                ["message"] = record.Message,
                ["occurred_at"] = record.OccurredAt,
            });
        }
        return new JsonObject
        {
            ["events"] = array,
            ["dropped"] = dropped,
            ["error"] = error,
        };
    }
}
