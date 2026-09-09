using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging.Abstractions;
using FleetHubAgent.Fleet;
using FleetHubAgent.Fleet.Executors;

namespace FleetHubAgent.Tests;

/// <summary>
/// The capability report the heartbeat carries (roadmap #22), and the three ways it goes wrong
/// silently.
///
/// **First: the platform slug.** The hub keys a release manifest on it. A slug the hub does not
/// recognise is stored as unknown, which reads as "has not said", which means the Windows train
/// -- so a typo here has this machine offered a win-x64 executable, and nothing anywhere says
/// so. That is the exact failure the old arrangement avoided by pinning this agent's version
/// below the hub's train floor, and this report is what replaces it.
///
/// **Second: drift.** The hub refuses any command a machine's reported list leaves out, so a
/// report that has fallen behind the executor set does not fail loudly -- it makes a command
/// this agent CAN run come back refused, with a console button that stays grey and nothing in
/// any log. Hence the list is derived from the dispatcher, and the test below builds one with a
/// novel executor and asserts the report follows it rather than asserting a fixed list.
///
/// **Third: field names.** hub/capabilities.py reads `platform`, `commands` and `features` and
/// quietly drops anything else. A renamed field produces a report the hub accepts, stores as
/// nothing, and then treats as "has not said" -- so the gate is simply off and the fleet
/// behaves exactly as before. A hub-side test cannot catch it either, because it only ever sees
/// what this side sends.
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
    public void The_platform_is_the_slug_the_hub_keys_a_manifest_on()
    {
        // hub/capabilities.py's PLATFORM_LINUX and hub/channels.py's manifest table. Restated
        // rather than read, because the hub is Python -- and asserted here because the cost of
        // a mismatch is this machine being offered the Windows agent's build.
        Assert.Equal("linux", AgentCapabilities.PlatformLinux);
        Assert.Equal("linux", AgentCapabilities.For(Dispatcher("rename")).Platform);
    }

    [Fact]
    public void The_reported_commands_are_the_dispatchers_own_executor_set()
    {
        var report = AgentCapabilities.For(Dispatcher("rename", "restart", "made_up_command"));
        Assert.Equal(["made_up_command", "rename", "restart"], report.Commands);
    }

    [Fact]
    public void The_command_list_is_sorted_and_deduplicated()
    {
        // Sorted because the hub compares the stored block against the reported one to decide
        // whether to write. An unstable order would make every heartbeat look like a change.
        var report = new AgentCapabilities("linux", ["restart", "rename", "restart"]);
        Assert.Equal(["rename", "restart"], report.Commands);
    }

    [Fact]
    public void An_agent_with_no_executors_reports_an_empty_list_rather_than_nothing()
    {
        // Empty and absent are different claims to the hub: absent permits everything, empty
        // permits nothing. An agent with no executors means the second.
        var json = AgentCapabilities.For(Dispatcher()).ToJson();
        Assert.NotNull(json["commands"]);
        Assert.Empty((JsonArray)json["commands"]!);
    }

    [Fact]
    public void The_json_uses_the_hubs_field_names()
    {
        var json = AgentCapabilities.For(Dispatcher("rename")).ToJson();
        Assert.Equal("linux", (string?)json["platform"]);
        Assert.IsType<JsonArray>(json["commands"]);
        Assert.IsType<JsonArray>(json["features"]);
        // Exactly three, and no more: a field the hub does not read is one somebody added
        // expecting it to arrive somewhere.
        Assert.Equal(3, json.Count);
    }

    [Fact]
    public void Features_are_empty_but_present()
    {
        // This agent claims no non-command feature today. Sending the key anyway is what makes
        // that a statement rather than a silence the hub would read as "permits everything".
        var json = AgentCapabilities.For(Dispatcher("rename")).ToJson();
        Assert.Empty((JsonArray)json["features"]!);
    }
}
