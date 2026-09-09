using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging.Abstractions;
using FleetHubAgent.Fleet;
using FleetHubAgent.Fleet.Executors;

namespace FleetHubAgent.Core.Tests;

/// <summary>
/// The capability report the heartbeat carries (roadmap #23), and the two ways it goes wrong
/// silently.
///
/// **First: drift.** The hub refuses any command a machine's reported list leaves out, so a
/// report that has fallen behind the executor set does not fail loudly -- it makes a command
/// this agent CAN run come back refused, with a console button that stays grey and nothing in
/// any log. That is why the list is derived from the dispatcher rather than written out, and
/// why the test below builds a dispatcher with a novel executor and asserts the report follows
/// it rather than asserting a fixed list.
///
/// **Second: field names.** hub/capabilities.py reads `platform`, `commands` and `features` and
/// quietly drops anything else, and it stores an unrecognised platform slug as unknown. Either
/// mistake produces a report the hub accepts, stores as nothing, and then treats as "this
/// machine has not said" -- which permits everything, so the gate this class exists to feed is
/// simply off and the fleet behaves exactly as it did before. There is no error anywhere.
/// A hub-side test cannot catch it either, because it only ever sees what this side sends.
/// </summary>
public class AgentCapabilitiesTests
{
    private sealed class StubExecutor(string type) : ICommandExecutor
    {
        public string Type => type;
        public Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput, CancellationToken ct) =>
            Task.FromResult(CommandResult.Ok("stub"));
    }

    private static CommandDispatcher Dispatcher(params string[] types) =>
        new(NullLogger<CommandDispatcher>.Instance, types.Select(t => new StubExecutor(t)));

    [Fact]
    public void The_reported_commands_are_the_dispatchers_own_executor_set()
    {
        var report = AgentCapabilities.For(Dispatcher("rename", "show_message"));

        // Asserted against the dispatcher rather than against a literal list, because a literal
        // would have to be edited in step with every new executor -- which is exactly the
        // drift this derivation exists to make impossible.
        Assert.Equal(new[] { "rename", "show_message" }, report.Commands);
    }

    [Fact]
    public void An_executor_added_later_reaches_the_report_with_no_second_edit()
    {
        var report = AgentCapabilities.For(Dispatcher("rename", "locate_device"));
        Assert.Contains("locate_device", report.Commands);
    }

    [Fact]
    public void The_command_order_is_stable_so_an_unchanged_report_looks_unchanged()
    {
        // The hub compares the stored block against the reported one to decide whether to
        // write. An order that depended on dictionary iteration would make every heartbeat
        // look like a change, and a fleet of phones would write a row each, every ten seconds,
        // forever -- with `reported_at` then meaning nothing.
        var first = AgentCapabilities.For(Dispatcher("rename", "show_message", "locate_device"));
        var second = AgentCapabilities.For(Dispatcher("locate_device", "rename", "show_message"));
        Assert.Equal(first.ToJson().ToJsonString(), second.ToJson().ToJsonString());
    }

    [Fact]
    public void The_platform_is_the_slug_the_hub_accepts()
    {
        // hub/capabilities.py's PLATFORMS tuple. Anything else is stored there as unknown,
        // which reads as "has not said" and turns every gate this feeds off -- with no error.
        Assert.Equal("android", AgentCapabilities.PlatformAndroid);
        Assert.Equal("android", AgentCapabilities.For(Dispatcher("rename")).Platform);
    }

    [Fact]
    public void The_json_carries_exactly_the_three_keys_the_hub_reads()
    {
        var json = (JsonObject)AgentCapabilities.For(Dispatcher("rename")).ToJson();

        Assert.Equal("android", json["platform"]!.GetValue<string>());
        Assert.Equal(new[] { "rename" },
            json["commands"]!.AsArray().Select(n => n!.GetValue<string>()));
        Assert.NotNull(json["features"]);
        // Extra keys are not an error at the hub -- they are dropped in silence -- so the count
        // is asserted here, where it can still be noticed.
        Assert.Equal(3, json.Count);
    }

    [Fact]
    public void An_empty_feature_list_is_still_sent_because_absent_and_empty_differ()
    {
        // The hub reads a MISSING list as "has not said" (which permits everything) and an
        // EMPTY one as a statement. Today this agent implements no features, and the honest
        // report is the empty list -- omitting the key would claim ignorance rather than
        // absence, and later hide a genuinely unimplemented feature behind "we do not know".
        var json = (JsonObject)AgentCapabilities.For(Dispatcher("rename")).ToJson();
        Assert.Empty(json["features"]!.AsArray());
    }

    [Fact]
    public void Reported_features_are_carried_and_deduplicated()
    {
        var report = AgentCapabilities.For(
            Dispatcher("rename"),
            [AgentCapabilities.FeatureLocate, AgentCapabilities.FeatureLocate]);
        Assert.Equal(new[] { "locate" }, report.Features);
    }

    [Fact]
    public void The_feature_slugs_match_the_hubs_own_constants()
    {
        // Mirrors hub/capabilities.py's FEATURE_* values. A slug that disagrees is not an
        // error at either end: the hub stores the string it was sent and the console asks for
        // a different one, so the feature is simply never seen to exist.
        Assert.Equal("locate", AgentCapabilities.FeatureLocate);
        Assert.Equal("app_policy", AgentCapabilities.FeatureAppPolicy);
        Assert.Equal("time_policy", AgentCapabilities.FeatureTimePolicy);
        Assert.Equal("device_owner", AgentCapabilities.FeatureDeviceOwner);
    }

    [Fact]
    public void Every_reported_name_is_one_the_hubs_parser_will_keep()
    {
        // capabilities._name() drops anything that is not alphanumeric-plus-underscore, and it
        // drops it SILENTLY -- a name with a hyphen or a space in it does not fail the report,
        // it just vanishes from the list, and the hub then refuses that command forever. So
        // the constraint is asserted on this side, where the name is chosen.
        var report = AgentCapabilities.For(
            Dispatcher("rename", "show_message", "locate_device"),
            [AgentCapabilities.FeatureLocate, AgentCapabilities.FeatureAppPolicy,
             AgentCapabilities.FeatureTimePolicy, AgentCapabilities.FeatureDeviceOwner]);

        foreach (var name in report.Commands.Concat(report.Features).Append(report.Platform))
        {
            Assert.False(string.IsNullOrWhiteSpace(name));
            Assert.True(name.Replace("_", "").All(char.IsAsciiLetterOrDigit),
                $"'{name}' would be dropped by the hub's capability parser");
            Assert.True(name.Length <= 64, $"'{name}' is longer than the hub keeps");
        }
    }

    [Fact]
    public void A_dispatcher_with_no_executors_reports_an_empty_list_not_a_missing_one()
    {
        // The state a stripped-down build would be in, and the one place the asymmetry bites:
        // an empty commands list tells the hub to refuse EVERYTHING for this machine, which is
        // correct here and would be catastrophic if it were ever the default for a machine that
        // simply had not reported. The two are different values on the wire, and this is the
        // test that says so from the sending side.
        var json = (JsonObject)AgentCapabilities.For(Dispatcher()).ToJson();
        Assert.Empty(json["commands"]!.AsArray());
    }
}
