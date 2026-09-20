using System.Diagnostics.Eventing.Reader;
using System.Globalization;

namespace TempMonitorAgent.Events;

/// <summary>
/// Reads matching records out of this machine's Windows event logs (roadmap #16). A local
/// read-only probe, like <c>BiosReader</c> and <c>NicReader</c> -- it decides nothing and
/// posts nothing.
///
/// <para><b>The filter is compiled into the query, not applied afterwards.</b> That is the
/// whole reason this feature is affordable. An XPath built from the subscriptions goes to
/// the Windows event service, which walks its own indexes and hands back only matches; the
/// alternative -- enumerate the channel and test each record in managed code -- reads every
/// record of every log on every pass, which on a domain controller is the agent doing more
/// work than everything else it does put together.</para>
///
/// <para><b>Only the provider substring is matched here</b>, after the query, because XPath
/// has equality on <c>Provider/@Name</c> and no contains(). By then the query has already
/// narrowed the set to the subscribed ids and levels, so what this tests is a handful of
/// records rather than a log.</para>
///
/// <para><b>The window is relative, not absolute.</b> The query asks for records written in
/// the last N milliseconds (<c>timediff(@SystemTime)</c>) rather than after a stored
/// timestamp. Absolute times mean formatting a UTC string that has to agree exactly with
/// what the event service parses, and a machine whose clock steps -- which is precisely the
/// machine somebody is investigating -- either loses records or re-reads an hour of them. A
/// relative window degrades gracefully: a clock step costs at most one duplicate pass, and
/// <c>RecordId</c> de-duplication (see EventLogReporter) absorbs that.</para>
///
/// <para><b>Nothing is back-filled.</b> A subscription added this afternoon does not go
/// through last month's Security log: the first pass reads one window like every other pass.
/// The rejected alternative was an initial catch-up, and it was turned down because the
/// first thing it would do on a fleet of two hundred PCs is deliver a month of records to a
/// SQLite hub, in one burst, at the exact moment somebody is trying to see whether the
/// feature works.</para>
/// </summary>
public static class WindowsEventReader
{
    /// <summary>One record, reduced to the wire shape the hub stores.</summary>
    /// <param name="Log">The channel it came from.</param>
    /// <param name="Provider">The provider that wrote it.</param>
    /// <param name="EventId">The event id.</param>
    /// <param name="Level">One of the hub's level slugs (see <see cref="LevelSlug"/>).</param>
    /// <param name="Message">The rendered message, truncated.</param>
    /// <param name="OccurredAt">Unix seconds, from the record's OWN timestamp.</param>
    /// <param name="RecordId">The channel-local record number, for de-duplication.</param>
    public sealed record Record(string Log, string Provider, int EventId, string Level,
                                string Message, long OccurredAt, long RecordId);

    /// <summary>Longest message kept. Matches events.MAX_MESSAGE_CHARS on the hub, which
    /// truncates again -- the tail of a long Windows message is boilerplate, and carrying
    /// kilobytes of it per record is how a bounded payload stops being bounded.</summary>
    private const int MaxMessageChars = 2000;

    /// <summary>Records one channel may yield in one pass. A second cap sits above this on
    /// the whole report (EventLogReporter); this one stops a single screaming channel from
    /// crowding out every other subscription's records in the same pass.</summary>
    private const int MaxRecordsPerChannel = 500;

    /// <summary>
    /// Read matching records from one channel.
    ///
    /// <paramref name="subscriptions"/> must all name this channel; the caller groups them,
    /// so one channel is queried once however many subscriptions point at it.
    /// </summary>
    public static List<Record> Read(string log,
                                    IReadOnlyList<EventSubscriptionStore.Subscription> subscriptions,
                                    int windowMillis)
    {
        var results = new List<Record>();
        if (subscriptions.Count == 0) return results;

        var query = new EventLogQuery(log, PathType.LogName, BuildXPath(subscriptions, windowMillis))
        {
            // Read newest first, so that hitting MaxRecordsPerChannel on a machine mid-storm
            // keeps the RECENT records rather than the oldest ones in the window. The list is
            // reversed before it is returned, so callers still see them in order.
            ReverseDirection = true,
            // A channel that does not exist on this machine (a provider channel from a
            // subscription aimed at servers, landing on a laptop) must not throw the whole
            // pass away. Tolerating it here means one absent channel costs its own records
            // and nothing else's.
            TolerateQueryErrors = true,
        };

        using var reader = new EventLogReader(query);
        for (EventRecord? record = reader.ReadEvent(); record is not null; record = reader.ReadEvent())
        {
            using (record)
            {
                if (results.Count >= MaxRecordsPerChannel) break;
                var provider = record.ProviderName ?? "";
                if (!MatchesProvider(provider, subscriptions)) continue;
                results.Add(new Record(
                    log,
                    provider,
                    record.Id,
                    LevelSlug(record.Level),
                    Truncate(SafeMessage(record)),
                    ToUnixSeconds(record.TimeCreated),
                    record.RecordId ?? 0));
            }
        }
        results.Reverse();
        return results;
    }

    /// <summary>
    /// The XPath one channel's subscriptions compile to.
    ///
    /// Shape: <c>*[System[(TimeCreated[timediff(@SystemTime) &lt;= N]) and (A or B)]]</c>,
    /// where each of A and B is one subscription's id/level clause. OR between subscriptions
    /// is what makes "everything anybody asked for on this channel" one query.
    ///
    /// Internal rather than private so the agent tests can assert on the string without a
    /// Windows event service anywhere near them -- the two halves of this feature are a C#
    /// query and a Python ingest, and a filter that silently matches nothing is exactly the
    /// failure neither half can see on its own.
    /// </summary>
    internal static string BuildXPath(IReadOnlyList<EventSubscriptionStore.Subscription> subscriptions,
                                      int windowMillis)
    {
        var window = Math.Max(1000, windowMillis);
        var clauses = new List<string>();
        foreach (var subscription in subscriptions)
        {
            var parts = new List<string>();
            var ids = Clause("EventID", subscription.EventIds.Select(id => id.ToString(
                CultureInfo.InvariantCulture)));
            if (ids is not null) parts.Add(ids);
            var levels = Clause("Level", LevelNumbers(subscription.Levels).Select(
                level => level.ToString(CultureInfo.InvariantCulture)));
            if (levels is not null) parts.Add(levels);
            // A subscription with neither ids nor levels matches the whole channel. That is a
            // legitimate thing to ask for (the console warns about the volume), and it makes
            // every other clause on this channel redundant -- so it short-circuits to the
            // time window alone rather than being OR'd in beside them.
            if (parts.Count == 0) return TimeOnly(window);
            clauses.Add(parts.Count == 1 ? parts[0] : "(" + string.Join(" and ", parts) + ")");
        }
        if (clauses.Count == 0) return TimeOnly(window);
        var matched = clauses.Count == 1 ? clauses[0] : "(" + string.Join(" or ", clauses) + ")";
        return $"*[System[TimeCreated[timediff(@SystemTime) <= {window}] and {matched}]]";
    }

    private static string TimeOnly(int windowMillis) =>
        $"*[System[TimeCreated[timediff(@SystemTime) <= {windowMillis}]]]";

    private static string? Clause(string field, IEnumerable<string> values)
    {
        var list = values.Distinct().ToList();
        if (list.Count == 0) return null;
        var terms = list.Select(value => $"{field}={value}");
        return list.Count == 1 ? terms.First() : "(" + string.Join(" or ", terms) + ")";
    }

    /// <summary>The Windows level numbers a set of hub slugs means.
    ///
    /// <c>information</c> covers BOTH 4 and 0. Level 0 is "LogAlways", what a provider emits
    /// when it declines to classify a record, and it is genuinely common -- Security channel
    /// audit records are level 0. Mapping it anywhere else, or nowhere, would make
    /// "collect Information from Security" the one subscription that returns nothing, which
    /// is the first thing anybody will try.</summary>
    internal static IEnumerable<int> LevelNumbers(IReadOnlyList<string> slugs)
    {
        foreach (var slug in slugs)
        {
            switch (slug.Trim().ToLowerInvariant())
            {
                case "critical": yield return 1; break;
                case "error": yield return 2; break;
                case "warning": yield return 3; break;
                case "information": yield return 4; yield return 0; break;
                case "verbose": yield return 5; break;
            }
        }
    }

    /// <summary>The hub slug a Windows level number means. The inverse of
    /// <see cref="LevelNumbers"/>, and it must stay the inverse: a record collected as
    /// Information and stored as something else would make the console's level filter
    /// disagree with the subscription that asked for it.</summary>
    internal static string LevelSlug(byte? level) => level switch
    {
        1 => "critical",
        2 => "error",
        3 => "warning",
        5 => "verbose",
        _ => "information",
    };

    private static bool MatchesProvider(string provider,
                                        IReadOnlyList<EventSubscriptionStore.Subscription> subscriptions)
    {
        // Any subscription with no provider filter admits the record, so the test is "does
        // ANY of them accept it" rather than "do ALL of them". The query already established
        // that it matched somebody's ids and levels; this only removes records that every
        // interested subscription named a different provider for.
        foreach (var subscription in subscriptions)
        {
            if (string.IsNullOrWhiteSpace(subscription.Provider)) return true;
            if (provider.Contains(subscription.Provider.Trim(),
                                  StringComparison.OrdinalIgnoreCase)) return true;
        }
        return false;
    }

    /// <summary>The rendered message, or a readable stand-in.
    ///
    /// <c>FormatDescription()</c> throws or returns null when the provider's message DLL is
    /// missing or unreadable -- routine for a provider that has been uninstalled, and routine
    /// for a service running as LocalSystem reading another product's channel. An empty
    /// message is still a real event, so the id and provider carry it: a dropped record
    /// would make the console disagree with Event Viewer about whether anything happened.</summary>
    private static string SafeMessage(EventRecord record)
    {
        try
        {
            var text = record.FormatDescription();
            if (!string.IsNullOrWhiteSpace(text)) return text.Trim();
        }
        catch (EventLogException) { /* fall through to the stand-in */ }
        catch (System.Security.SecurityException) { /* likewise */ }
        return $"{record.ProviderName} event {record.Id}";
    }

    private static string Truncate(string text)
    {
        if (text.Length <= MaxMessageChars) return text;
        // Ellipsis rather than a hard cut, so nobody reads a truncated sentence as the whole
        // message and concludes the event says something it does not.
        return string.Concat(text.AsSpan(0, MaxMessageChars - 1), "…");
    }

    private static long ToUnixSeconds(DateTime? when)
    {
        if (when is null) return DateTimeOffset.UtcNow.ToUnixTimeSeconds();
        return new DateTimeOffset(DateTime.SpecifyKind(when.Value, DateTimeKind.Local)
            .ToUniversalTime(), TimeSpan.Zero).ToUnixTimeSeconds();
    }
}
