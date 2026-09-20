using System.Text.Json.Nodes;

namespace TempMonitorAgent.Events;

/// <summary>
/// What this machine has been told to collect from its Windows event logs (roadmap #16),
/// and the version of that instruction.
///
/// <para><b>In memory only, deliberately.</b> Every other hub-supplied document the agent
/// holds -- runtime config, the release channel -- is persisted, because losing it changes
/// behaviour in a direction somebody would notice late. Losing this one changes nothing that
/// matters: the version resets to empty on a service restart, the next heartbeat therefore
/// carries no version the hub recognises, and the hub sends the document again on that same
/// reply. One extra document per restart is cheaper than a state file that can go stale
/// against a hub that changed while the service was stopped.</para>
///
/// <para><b>An EMPTY document is a real instruction and is applied.</b> It is the release
/// valve: deleting or disabling the last subscription has to be able to STOP collection.
/// What is never applied is an ABSENT block -- that is a hub with nothing to say (because the
/// version matched) or a hub too old to know about this feature, and treating either as
/// "collect nothing" would make the feature stop working every time it was already
/// working.</para>
/// </summary>
public static class EventSubscriptionStore
{
    /// <summary>One subscription, reduced to what the agent matches on. The console's name,
    /// author and timestamps never reach here -- they cannot change what is collected, and a
    /// document carrying them would change version every time somebody renamed one.</summary>
    /// <param name="Id">The hub's id. Carried so a future report can say which subscription
    /// matched; nothing on the agent keys off it today.</param>
    /// <param name="Log">The channel, as Windows names it ("Security",
    /// "Microsoft-Windows-TaskScheduler/Operational").</param>
    /// <param name="EventIds">Event ids to match, or empty for any id.</param>
    /// <param name="Levels">Level slugs to match, or empty for any level.</param>
    /// <param name="Provider">A case-insensitive substring of the provider name, or empty.</param>
    public sealed record Subscription(string Id, string Log, IReadOnlyList<int> EventIds,
                                      IReadOnlyList<string> Levels, string Provider);

    private static readonly Lock Gate = new();
    private static string _version = "";
    private static IReadOnlyList<Subscription> _subscriptions = [];
    private static int _maxPerReport = 200;

    /// <summary>The version currently held, sent on every heartbeat so the hub can stay
    /// silent while it matches. Empty means "nothing applied yet", which no hub document
    /// hashes to, so the first heartbeat after a restart always gets the document.</summary>
    public static string Version
    {
        get { lock (Gate) return _version; }
    }

    public static IReadOnlyList<Subscription> Current
    {
        get { lock (Gate) return _subscriptions; }
    }

    /// <summary>The hub's cap on how many records one report may carry. Held here rather
    /// than compiled in so the hub can lower it fleet-wide without an agent release; the hub
    /// caps again on ingest regardless, because this is a courtesy and not a boundary.</summary>
    public static int MaxPerReport
    {
        get { lock (Gate) return _maxPerReport; }
    }

    /// <summary>Apply a document from a heartbeat reply. Returns true when it changed.
    ///
    /// Callers pass the parsed array and the version together; a version with no array, or
    /// an array with no version, is not a document and is ignored -- see
    /// <see cref="FromHeartbeat"/>, which is where that decision is made.</summary>
    public static bool Apply(string version, IReadOnlyList<Subscription> subscriptions,
                             int maxPerReport)
    {
        lock (Gate)
        {
            if (_version == version) return false;
            _version = version;
            _subscriptions = subscriptions;
            if (maxPerReport > 0) _maxPerReport = maxPerReport;
            return true;
        }
    }

    /// <summary>Read a document out of a heartbeat body, or null when it carried none.
    ///
    /// Static and string-in so the whole wire contract can be tested without a hub, the way
    /// <c>FleetClient.ShouldReenrollAfter401</c> is. Anything malformed comes back null
    /// rather than as an empty document: a truncated reply must never read as "stop
    /// collecting", which is the one misreading that would silently turn the feature off
    /// across a fleet.</summary>
    public static (string Version, List<Subscription> Subscriptions, int MaxPerReport)? FromHeartbeat(
        string body)
    {
        // ONE try around the whole parse, unlike the field-by-field handling elsewhere in
        // this agent. Every failure here has the same answer -- "the hub sent no document I
        // can read", which changes nothing -- and the alternative (a catch per field) would
        // eventually let a half-read document through, which is the one outcome that must
        // not happen: a document missing half its subscriptions silently narrows what a
        // fleet collects.
        try
        {
            var root = JsonNode.Parse(body);
            var version = root?["event_subscriptions_version"]?.GetValue<string>();
            if (string.IsNullOrEmpty(version)) return null;
            if (root?["event_subscriptions"] is not JsonArray array) return null;

            var parsed = new List<Subscription>();
            foreach (var entry in array)
            {
                if (entry is not JsonObject item) continue;
                var log = item["log"]?.GetValue<string>();
                // A channel-less entry is dropped rather than kept as "": an empty channel is
                // not a query, and keeping it would put a failing read on every pass forever.
                if (string.IsNullOrWhiteSpace(log)) continue;
                parsed.Add(new Subscription(
                    item["id"]?.GetValue<string>() ?? "",
                    log,
                    ReadInts(item["event_ids"]),
                    ReadStrings(item["levels"]),
                    item["provider"]?.GetValue<string>() ?? ""));
            }

            var max = 0;
            try { max = root?["event_max_per_report"]?.GetValue<int>() ?? 0; }
            catch (Exception e) when (e is InvalidOperationException or FormatException)
            {
                // A cap that is not a number is no cap, and that is not worth losing the
                // subscriptions over -- the hub caps again on ingest regardless.
            }
            return (version, parsed, max);
        }
        catch (Exception e) when (e is System.Text.Json.JsonException
                                       or InvalidOperationException or FormatException)
        {
            return null;
        }
    }

    private static List<int> ReadInts(JsonNode? node)
    {
        var values = new List<int>();
        if (node is not JsonArray array) return values;
        foreach (var entry in array)
        {
            try
            {
                if (entry is not null) values.Add(entry.GetValue<int>());
            }
            catch (Exception e) when (e is InvalidOperationException or FormatException)
            {
                // One unreadable id must not discard the rest of the filter. Skipping it
                // NARROWS what is collected rather than widening it, which is the safe
                // direction for a document that decides how much this machine sends.
            }
        }
        return values;
    }

    private static List<string> ReadStrings(JsonNode? node)
    {
        var values = new List<string>();
        if (node is not JsonArray array) return values;
        foreach (var entry in array)
        {
            var text = entry?.GetValue<string>();
            if (!string.IsNullOrWhiteSpace(text)) values.Add(text);
        }
        return values;
    }
}
