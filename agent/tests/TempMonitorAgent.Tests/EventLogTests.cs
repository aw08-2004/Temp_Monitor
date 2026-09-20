using System.Text.Json.Nodes;
using TempMonitorAgent.Events;
using Xunit;

namespace TempMonitorAgent.Tests;

/// <summary>
/// The three halves of roadmap #16 that can be pinned exactly: the XPath a subscription set
/// compiles to, the level mapping, and the two wire contracts (the document coming down, the
/// report going up).
///
/// None of them touches the Windows event service. The query execution is
/// environment-dependent by nature -- it needs channels, records and a service to read them
/// -- but these four are pure, AND they are the places where a mistake is SILENT. A filter
/// that matches nothing looks exactly like a quiet fleet; a level that maps to no number
/// makes one subscription collect nothing while the others work; an absent document read as
/// an empty one turns collection off across a fleet with nothing in any log to say so.
/// </summary>
public class EventLogTests
{
    private static EventSubscriptionStore.Subscription Sub(
        string log = "Security", int[]? ids = null, string[]? levels = null,
        string provider = "") =>
        new("id-1", log, ids ?? [], levels ?? [], provider);

    [Fact]
    public void Compiles_ids_and_levels_into_one_query()
    {
        var xpath = WindowsEventReader.BuildXPath(
            [Sub(ids: [4625, 4740], levels: ["error"])], 60000);
        Assert.Contains("EventID=4625", xpath);
        Assert.Contains("EventID=4740", xpath);
        Assert.Contains("Level=2", xpath);
        Assert.Contains("timediff(@SystemTime) <= 60000", xpath);
    }

    [Fact]
    public void Ors_several_subscriptions_on_one_channel()
    {
        // The failure this catches: querying once per subscription, which is N round trips to
        // the event service per pass instead of one per channel.
        var xpath = WindowsEventReader.BuildXPath(
            [Sub(ids: [4625]), Sub(ids: [7045])], 60000);
        Assert.Contains("EventID=4625", xpath);
        Assert.Contains("EventID=7045", xpath);
        Assert.Contains(" or ", xpath);
    }

    [Fact]
    public void A_subscription_with_no_filter_collapses_to_the_time_window()
    {
        // "Everything on this channel" makes every other clause on it redundant. Building
        // `(nothing) or (EventID=4625)` instead would be a query that matches LESS than the
        // subscription asked for, which is the one direction nobody would notice.
        var xpath = WindowsEventReader.BuildXPath([Sub(ids: [4625]), Sub()], 60000);
        Assert.DoesNotContain("EventID", xpath);
        Assert.Contains("timediff(@SystemTime)", xpath);
    }

    [Fact]
    public void Information_covers_level_zero_as_well_as_four()
    {
        // Security audit records are level 0 ("LogAlways"). Mapping Information to 4 alone
        // makes "collect Information from Security" -- the first subscription anybody writes
        // -- the one that returns nothing.
        var numbers = WindowsEventReader.LevelNumbers(["information"]).ToList();
        Assert.Contains(4, numbers);
        Assert.Contains(0, numbers);
    }

    [Fact]
    public void The_level_slug_is_the_inverse_of_the_level_number()
    {
        Assert.Equal("critical", WindowsEventReader.LevelSlug(1));
        Assert.Equal("error", WindowsEventReader.LevelSlug(2));
        Assert.Equal("warning", WindowsEventReader.LevelSlug(3));
        Assert.Equal("information", WindowsEventReader.LevelSlug(4));
        Assert.Equal("verbose", WindowsEventReader.LevelSlug(5));
        // 0 and an absent level both read as information, matching LevelNumbers above -- a
        // record collected as Information and stored as something else would make the
        // console's filter disagree with the subscription that asked for it.
        Assert.Equal("information", WindowsEventReader.LevelSlug(0));
        Assert.Equal("information", WindowsEventReader.LevelSlug(null));
    }

    [Fact]
    public void An_empty_document_is_a_document()
    {
        // THE RELEASE VALVE. Deleting the last subscription reaches the agent as an empty
        // array with a version, and it has to be applied -- reading it as "the hub said
        // nothing" leaves the machine collecting the Security channel forever.
        var parsed = EventSubscriptionStore.FromHeartbeat(
            """{"event_subscriptions_version":"abc123","event_subscriptions":[]}""");
        Assert.NotNull(parsed);
        Assert.Empty(parsed!.Value.Subscriptions);
        Assert.Equal("abc123", parsed.Value.Version);
    }

    [Fact]
    public void An_absent_block_is_not_a_document()
    {
        // The other half of the same decision, and the one that would break the feature the
        // other way: a steady-state heartbeat carries no document because the version
        // matched, and reading that as "collect nothing" would switch collection off on
        // every reply.
        Assert.Null(EventSubscriptionStore.FromHeartbeat("""{"status":"ok"}"""));
        Assert.Null(EventSubscriptionStore.FromHeartbeat("not json at all"));
        Assert.Null(EventSubscriptionStore.FromHeartbeat(
            """{"event_subscriptions":[{"log":"Security"}]}"""));
    }

    [Fact]
    public void Reads_a_subscription_document()
    {
        var parsed = EventSubscriptionStore.FromHeartbeat("""
        {"event_subscriptions_version":"v2","event_max_per_report":50,
         "event_subscriptions":[
           {"id":"a","log":"Security","event_ids":[4625],"levels":["error"],"provider":"Auditing"},
           {"id":"b","log":"","event_ids":[],"levels":[],"provider":""}]}
        """);
        Assert.NotNull(parsed);
        // The channel-less entry is dropped rather than collected as "": an empty channel is
        // not a query, and keeping it would put a failing read on every pass forever.
        Assert.Single(parsed!.Value.Subscriptions);
        Assert.Equal("Security", parsed.Value.Subscriptions[0].Log);
        Assert.Equal("Auditing", parsed.Value.Subscriptions[0].Provider);
        Assert.Equal(50, parsed.Value.MaxPerReport);
    }

    [Fact]
    public void The_report_carries_an_empty_list_rather_than_omitting_it()
    {
        // The whole reason the payload is an object. An empty report from a healthy machine
        // is what tells the hub the collector is alive; a bare array would be dropped by the
        // `is not None` shape on both sides the moment it went empty.
        var payload = EventLogReporter.ToPayload([], dropped: 0, error: null);
        Assert.True(payload.ContainsKey("events"));
        Assert.Empty(payload["events"]!.AsArray());
    }

    [Fact]
    public void The_report_names_what_it_dropped_and_what_failed()
    {
        var record = new WindowsEventReader.Record(
            "Security", "Microsoft-Windows-Security-Auditing", 4625, "information",
            "An account failed to log on.", 1_700_000_000, 42);
        var payload = EventLogReporter.ToPayload([record], dropped: 12,
                                                 error: "Setup: access denied");
        Assert.Equal(12, payload["dropped"]!.GetValue<int>());
        Assert.Equal("Setup: access denied", payload["error"]!.GetValue<string>());
        var first = payload["events"]!.AsArray()[0]!;
        Assert.Equal(4625, first["event_id"]!.GetValue<int>());
        Assert.Equal(1_700_000_000L, first["occurred_at"]!.GetValue<long>());
        // RecordId is deliberately NOT on the wire: it is channel-local and resets when a log
        // is cleared, so it de-duplicates within one agent's memory and means nothing to the
        // hub. Sending it would invite somebody to key on it.
        Assert.Null(first["record_id"]);
    }
}
