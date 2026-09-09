using System.Text.Json.Nodes;
using FleetHubAgent.Fleet;

namespace FleetHubAgent.Core.Tests;

/// <summary>
/// The change-only inventory pattern, and the three ways it goes wrong without saying so.
///
/// **A block lost to a failed heartbeat is the one this file exists for.** The Windows agent's
/// reporters record a payload as sent at the moment they hand it over, so a heartbeat that
/// fails a second later leaves the agent convinced the hub has it -- and because the next read
/// finds the same content, the block is never re-sent until it changes again. The result is an
/// inventory in the console that is stale, with nothing anywhere saying so and no way to tell
/// it from a device that simply has not changed. This implementation only marks a block sent
/// after a heartbeat succeeds, and these tests are what hold it there.
///
/// **"Became empty" must be a change like any other.** A device that had forty apps and now has
/// none has to report that, and on Windows the equivalent transition is the only honest evidence
/// a patch install worked. Any truthiness check anywhere along the path turns that one report
/// into silence, which is why the payload is an object wrapping the list rather than the list.
///
/// **A failed READ must not advance the interval.** A source that could not look should be
/// asked again on the next tick, not in fifteen minutes -- and it must not be mistaken for a
/// source that looked and found nothing.
/// </summary>
public class InventoryReporterTests
{
    private static readonly DateTimeOffset T0 =
        new(2026, 9, 7, 12, 0, 0, TimeSpan.Zero);

    private static JsonObject Apps(params string[] packages)
    {
        var list = new JsonArray();
        foreach (var package in packages) list.Add(new JsonObject { ["package"] = package });
        return new JsonObject { ["apps"] = list };
    }

    private static InventoryReporter Build(int minutes = 15) =>
        new("apps", TimeSpan.FromMinutes(minutes));

    [Fact]
    public void A_fresh_reading_becomes_pending()
    {
        var reporter = Build();
        reporter.Offer(Apps("a"), T0);
        Assert.NotNull(reporter.Pending());
    }

    [Fact]
    public void Nothing_is_pending_before_the_first_reading()
    {
        Assert.Null(Build().Pending());
    }

    [Fact]
    public void Looking_at_a_payload_does_not_consume_it()
    {
        // The assertion the whole file is about: a heartbeat that never reached the hub must
        // leave the block exactly where it was.
        var reporter = Build();
        reporter.Offer(Apps("a"), T0);
        var first = reporter.Pending();
        Assert.NotNull(first);
        Assert.NotNull(reporter.Pending());
        Assert.NotNull(reporter.Pending());
    }

    [Fact]
    public void A_block_stops_being_sent_only_once_a_heartbeat_has_acknowledged_it()
    {
        var reporter = Build();
        reporter.Offer(Apps("a"), T0);
        var payload = reporter.Pending()!;
        reporter.MarkSent(payload);
        Assert.Null(reporter.Pending());
    }

    [Fact]
    public void An_unchanged_reading_is_not_sent_again()
    {
        var reporter = Build(minutes: 0);
        reporter.Offer(Apps("a", "b"), T0);
        reporter.MarkSent(reporter.Pending()!);

        // Same content, read again later. Nothing to say.
        reporter.Offer(Apps("a", "b"), T0.AddMinutes(20));
        Assert.Null(reporter.Pending());
    }

    [Fact]
    public void A_changed_reading_is_sent()
    {
        var reporter = Build(minutes: 0);
        reporter.Offer(Apps("a"), T0);
        reporter.MarkSent(reporter.Pending()!);

        reporter.Offer(Apps("a", "b"), T0.AddMinutes(20));
        Assert.NotNull(reporter.Pending());
    }

    [Fact]
    public void Becoming_EMPTY_is_a_change_and_is_sent()
    {
        // The report that any truthiness check would swallow. A device that has had everything
        // uninstalled has to be able to say so.
        var reporter = Build(minutes: 0);
        reporter.Offer(Apps("a", "b"), T0);
        reporter.MarkSent(reporter.Pending()!);

        reporter.Offer(Apps(), T0.AddMinutes(20));
        var payload = reporter.Pending();
        Assert.NotNull(payload);
        Assert.Empty(payload!["apps"]!.AsArray());
    }

    [Fact]
    public void The_payload_is_an_object_wrapping_the_list_not_the_list()
    {
        // Why the wrapper exists at all: hub/fleet_web.py tests the key with `is not None`, and
        // an object stays truthy when the list inside it is empty.
        var reporter = Build();
        reporter.Offer(Apps(), T0);
        Assert.IsType<JsonObject>(reporter.Pending());
    }

    [Fact]
    public void A_failed_heartbeat_leaves_the_block_to_be_sent_again()
    {
        // The whole scenario, end to end: read, attach, the heartbeat fails (so nothing
        // acknowledges), read again with the same content, and the block is still owed.
        var reporter = Build(minutes: 0);
        reporter.Offer(Apps("a"), T0);
        _ = reporter.Pending();                       // attached to a heartbeat that failed
        reporter.Offer(Apps("a"), T0.AddMinutes(20)); // the content has not changed

        var payload = reporter.Pending();
        Assert.NotNull(payload);
        Assert.Equal("a", payload!["apps"]!.AsArray()[0]!["package"]!.GetValue<string>());
    }

    [Fact]
    public void A_failed_READ_offers_nothing_and_does_not_advance_the_interval()
    {
        var reporter = Build(minutes: 15);
        reporter.Offer(null, T0);
        Assert.Null(reporter.Pending());
        // Still due: a source that could not look is asked again on the next tick, not in
        // fifteen minutes. Reading null as "looked and found nothing" would be the far worse
        // mistake -- it would wipe a good inventory at the hub.
        Assert.True(reporter.IsDue(T0));
    }

    [Fact]
    public void A_successful_read_puts_the_source_back_to_sleep()
    {
        var reporter = Build(minutes: 15);
        Assert.True(reporter.IsDue(T0));
        reporter.Offer(Apps("a"), T0);
        Assert.False(reporter.IsDue(T0.AddMinutes(5)));
        Assert.True(reporter.IsDue(T0.AddMinutes(16)));
    }

    [Fact]
    public void Invalidate_makes_it_due_again_and_re_sends_unchanged_content()
    {
        // What is called after an app policy is applied: the console must not show yesterday's
        // installed list beside today's policy for the rest of the interval.
        var reporter = Build(minutes: 15);
        reporter.Offer(Apps("a"), T0);
        reporter.MarkSent(reporter.Pending()!);
        Assert.False(reporter.IsDue(T0.AddMinutes(1)));

        reporter.Invalidate();
        Assert.True(reporter.IsDue(T0.AddMinutes(1)));
        reporter.Offer(Apps("a"), T0.AddMinutes(1));   // identical content
        Assert.NotNull(reporter.Pending());            // ...and it is sent anyway
    }

    [Fact]
    public void Acknowledging_a_payload_that_is_no_longer_pending_does_not_swallow_a_newer_one()
    {
        // The race: a re-read lands between attaching a block and the heartbeat coming back.
        // Marking the OLD payload sent must not clear the new one, or that change is lost with
        // nothing anywhere to notice it.
        var reporter = Build(minutes: 0);
        reporter.Offer(Apps("a"), T0);
        var inFlight = reporter.Pending()!;
        reporter.Offer(Apps("a", "b"), T0.AddSeconds(1));

        reporter.MarkSent(inFlight);
        var still = reporter.Pending();
        Assert.NotNull(still);
        Assert.Equal(2, still!["apps"]!.AsArray().Count);
    }

    [Fact]
    public void The_key_is_the_heartbeat_block_name()
    {
        // A mismatch with hub/fleet_web.py's ingest is a block the hub accepts, stores nowhere,
        // and never mentions.
        Assert.Equal("apps", Build().Key);
    }
}
