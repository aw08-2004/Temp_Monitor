using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging.Abstractions;
using FleetHubAgent.Fleet;
using FleetHubAgent.Fleet.Executors;

namespace FleetHubAgent.Core.Tests;

/// <summary>
/// The locate command, and the two things about it that would fail silently.
///
/// **"No fix" must be a SUCCESS.** A device indoors, with location switched off, or with the
/// permission never granted has answered the question truthfully. Reported as a failure it
/// becomes indistinguishable in the console from a network problem or a crashed agent, and an
/// operator hunting a lost phone spends their time on the wrong thing. The distinction lives
/// only in a boolean nobody looks at until it matters, which is exactly the kind of thing that
/// gets "simplified" away.
///
/// **The wire keys are a contract with hub/location.py's clean_fix**, and Python cannot import
/// C#. A renamed key here does not crash anything: the hub parses the object, finds no `lat`,
/// and files a perfectly good fix as "the device did not report a position" -- forever, for
/// every device, with nothing in any log. So the keys are asserted by name on this side.
///
/// The third assertion is the disclosure. The device tells the person holding it who asked,
/// every time, INCLUDING when the answer is "I do not know" -- otherwise a failed locate would
/// be the quiet way to check whether somebody's phone is switched on.
/// </summary>
public class LocateDeviceExecutorTests
{
    private sealed class StubSource(LocationFix? fix, string? reason = null) : ILocationSource
    {
        public TimeSpan? Budget { get; private set; }
        public string? UnavailableReason => reason;
        public Task<LocationFix?> GetCurrentAsync(TimeSpan budget, CancellationToken ct)
        {
            Budget = budget;
            return Task.FromResult(fix);
        }
    }

    private sealed class ThrowingSource : ILocationSource
    {
        public string? UnavailableReason => null;
        public Task<LocationFix?> GetCurrentAsync(TimeSpan budget, CancellationToken ct) =>
            throw new InvalidOperationException("provider exploded");
    }

    private sealed class SpyDisclosure : ILocationDisclosure
    {
        public int Calls { get; private set; }
        public string? LastOperator { get; private set; }
        public void NotifyLocated(string? requestedBy)
        {
            Calls += 1;
            LastOperator = requestedBy;
        }
    }

    private static readonly LocationFix Fix = new(
        Latitude: -22.3489, Longitude: -60.0331, AccuracyMetres: 12.5,
        Provider: "gps", FixedAtUnix: 1_900_000_000, Stale: false);

    private static (LocateDeviceExecutor, SpyDisclosure) Build(ILocationSource source)
    {
        var disclosure = new SpyDisclosure();
        return (new LocateDeviceExecutor(NullLogger<LocateDeviceExecutor>.Instance, source,
                                         disclosure), disclosure);
    }

    private static FleetCommand Command(JsonObject? parameters = null, string issuedBy = "op@x.com")
        => new() { Id = "c1", Type = "locate_device", Params = parameters, IssuedBy = issuedBy };

    [Fact]
    public void The_type_matches_the_hubs_command_type()
    {
        // hub/location.py's COMMAND_TYPE. A mismatch routes the command nowhere and answers
        // "unsupported" to a console that is showing the button.
        var (executor, _) = Build(new StubSource(Fix));
        Assert.Equal("locate_device", executor.Type);
    }

    [Fact]
    public async Task A_fix_comes_back_as_the_keys_the_hub_parses()
    {
        var (executor, _) = Build(new StubSource(Fix));
        var result = await executor.ExecuteAsync(Command(), null, CancellationToken.None);

        Assert.True(result.Success);
        var json = JsonNode.Parse(result.Output!)!.AsObject();
        Assert.Equal(-22.3489, json["lat"]!.GetValue<double>(), 4);
        Assert.Equal(-60.0331, json["lon"]!.GetValue<double>(), 4);
        Assert.Equal(12.5, json["accuracy_m"]!.GetValue<double>(), 4);
        Assert.Equal("gps", json["provider"]!.GetValue<string>());
        Assert.Equal(1_900_000_000, json["fixed_at"]!.GetValue<long>());
        Assert.False(json["stale"]!.GetValue<bool>());
    }

    [Fact]
    public async Task A_negative_coordinate_survives_the_round_trip()
    {
        // Both coordinates in the test fix are negative on purpose: the fleet is in Paraguay,
        // and a sign lost anywhere in this chain puts every device in Kazakhstan without
        // anything looking wrong.
        var (executor, _) = Build(new StubSource(Fix));
        var result = await executor.ExecuteAsync(Command(), null, CancellationToken.None);
        var json = JsonNode.Parse(result.Output!)!.AsObject();
        Assert.True(json["lat"]!.GetValue<double>() < 0);
        Assert.True(json["lon"]!.GetValue<double>() < 0);
    }

    [Fact]
    public async Task A_stale_fix_says_so()
    {
        // The most important field in the payload: a last-known position two hours old shown
        // as current is somebody driving to where a phone used to be.
        var stale = Fix with { Stale = true, Provider = "network" };
        var (executor, _) = Build(new StubSource(stale));
        var result = await executor.ExecuteAsync(Command(), null, CancellationToken.None);
        Assert.True(JsonNode.Parse(result.Output!)!["stale"]!.GetValue<bool>());
    }

    [Fact]
    public async Task An_unknown_accuracy_is_null_and_not_zero()
    {
        // Zero metres is a claim of perfect precision, and the console draws a circle from it.
        var vague = Fix with { AccuracyMetres = null };
        var (executor, _) = Build(new StubSource(vague));
        var result = await executor.ExecuteAsync(Command(), null, CancellationToken.None);
        var accuracy = JsonNode.Parse(result.Output!)!["accuracy_m"];
        Assert.Null(accuracy);
    }

    [Fact]
    public async Task No_fix_is_a_SUCCESS_with_the_reason()
    {
        // The assertion this file exists for. A Fail here is indistinguishable from a network
        // problem, and sends an operator looking at the agent instead of at the device.
        var (executor, _) = Build(new StubSource(null, "location is switched off on this device"));
        var result = await executor.ExecuteAsync(Command(), null, CancellationToken.None);

        Assert.True(result.Success);
        var json = JsonNode.Parse(result.Output!)!.AsObject();
        Assert.Equal("location is switched off on this device", json["error"]!.GetValue<string>());
        Assert.Null(json["lat"]);
    }

    [Fact]
    public async Task No_fix_and_no_reason_still_says_something_usable()
    {
        var (executor, _) = Build(new StubSource(null));
        var result = await executor.ExecuteAsync(Command(), null, CancellationToken.None);
        Assert.True(result.Success);
        Assert.Contains("no position", JsonNode.Parse(result.Output!)!["error"]!.GetValue<string>());
    }

    [Fact]
    public async Task A_provider_that_throws_is_a_FAILURE_unlike_a_missing_fix()
    {
        // The other half of the distinction: the executor could not run, which the hub files
        // as "no answer" and shows differently from "no fix".
        var (executor, _) = Build(new ThrowingSource());
        var result = await executor.ExecuteAsync(Command(), null, CancellationToken.None);
        Assert.False(result.Success);
        Assert.Contains("provider exploded", result.Output);
    }

    [Fact]
    public async Task The_device_is_told_who_asked()
    {
        var (executor, disclosure) = Build(new StubSource(Fix));
        await executor.ExecuteAsync(Command(issuedBy: "alex@example.com"), null,
                                    CancellationToken.None);
        Assert.Equal(1, disclosure.Calls);
        Assert.Equal("alex@example.com", disclosure.LastOperator);
    }

    [Fact]
    public async Task The_device_is_told_even_when_there_is_no_fix_to_give()
    {
        // Otherwise a failed locate becomes the quiet way to check whether somebody's phone is
        // switched on, and the disclosure stops being a promise.
        var (executor, disclosure) = Build(new StubSource(null, "no fix"));
        await executor.ExecuteAsync(Command(), null, CancellationToken.None);
        Assert.Equal(1, disclosure.Calls);
    }

    [Fact]
    public void The_time_budget_is_the_hubs_when_it_sends_one()
    {
        Assert.Equal(20, LocateDeviceExecutor.ResolveTimeout(new JsonObject { ["timeout_seconds"] = 20 }));
    }

    [Fact]
    public void An_absent_budget_falls_back_to_the_hubs_own_default()
    {
        // Matches location.default_timeout_seconds' default, so a device behaves the same
        // whether or not the console told it what to do.
        Assert.Equal(45, LocateDeviceExecutor.ResolveTimeout(null));
        Assert.Equal(45, LocateDeviceExecutor.ResolveTimeout(new JsonObject()));
    }

    [Fact]
    public void An_absurd_budget_is_clamped_at_both_ends()
    {
        // The ceiling matters more than the floor: this holds a wake lock on a battery-powered
        // device somebody is carrying, and no console setting should make that ten minutes.
        Assert.Equal(5, LocateDeviceExecutor.ResolveTimeout(new JsonObject { ["timeout_seconds"] = 0 }));
        Assert.Equal(5, LocateDeviceExecutor.ResolveTimeout(new JsonObject { ["timeout_seconds"] = -60 }));
        Assert.Equal(300, LocateDeviceExecutor.ResolveTimeout(new JsonObject { ["timeout_seconds"] = 99999 }));
    }

    [Fact]
    public void A_budget_of_the_wrong_json_type_does_not_take_the_command_down()
    {
        // params is JSON from the hub; a console that sends "20" rather than 20 should cost
        // that one field its value, not leave the command unanswered.
        Assert.Equal(20, LocateDeviceExecutor.ResolveTimeout(new JsonObject { ["timeout_seconds"] = "20" }));
        Assert.Equal(45, LocateDeviceExecutor.ResolveTimeout(new JsonObject { ["timeout_seconds"] = "soon" }));
    }

    [Fact]
    public async Task The_resolved_budget_is_what_reaches_the_provider()
    {
        var source = new StubSource(Fix);
        var (executor, _) = Build(source);
        await executor.ExecuteAsync(Command(new JsonObject { ["timeout_seconds"] = 99999 }), null,
                                    CancellationToken.None);
        Assert.Equal(TimeSpan.FromSeconds(300), source.Budget);
    }
}
