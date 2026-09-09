using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging.Abstractions;
using FleetHubAgent.Fleet.Executors;
using FleetHubAgent.State;

namespace FleetHubAgent.Core.Tests;

/// <summary>
/// Catches the rename that leaves a permanent duplicate in the console.
///
/// The hub keys a machine on the name it reports, so a renamed device arrives as a machine the
/// hub has never seen. What merges it back with the one it used to be is resolve_serial_group()
/// matching on serial_number -- and on Android that field carries the SSAID, which some devices
/// do not give up. A rename performed on such a device cannot be undone by anything the hub
/// does: there are simply two machines from then on.
///
/// So RenameExecutor refuses rather than warning, and this file is what keeps it refusing.
/// </summary>
public class RenameExecutorTests
{
    private static (RenameExecutor Executor, MachineNameProvider Names) Build(
        string? serial, FakeStateStore? store = null)
    {
        store ??= new FakeStateStore();
        var names = new MachineNameProvider(new AgentState(store), "Pixel-9-abcd1234");
        var identity = new SystemIdentity { SerialNumber = serial };
        return (new RenameExecutor(NullLogger<RenameExecutor>.Instance, names, identity), names);
    }

    private static FleetCommand Rename(string? newName) => new()
    {
        Id = "cmd-1",
        Type = "rename",
        Params = newName is null ? new JsonObject() : new JsonObject { ["new_name"] = newName },
    };

    [Fact]
    public async Task Refuses_when_the_device_has_no_stable_id_to_merge_on()
    {
        var (executor, names) = Build(serial: null);

        var result = await executor.ExecuteAsync(Rename("workshop-tablet-3"), null, default);

        Assert.False(result.Success);
        Assert.Contains("two", result.Output);          // names the actual consequence
        Assert.Equal("Pixel-9-abcd1234", names.Current); // and changed nothing
    }

    [Fact]
    public async Task Renames_when_there_is_a_stable_id()
    {
        var (executor, names) = Build(serial: "9f8e7d6c5b4a3210");

        var result = await executor.ExecuteAsync(Rename("workshop-tablet-3"), null, default);

        Assert.True(result.Success);
        Assert.Equal("workshop-tablet-3", names.Current);
    }

    [Fact]
    public async Task Says_out_loud_that_the_devices_own_name_did_not_change()
    {
        // The one command whose effect an operator will look for in the wrong place. Android
        // has no hostname an app may set, so Settings still shows the old name.
        var (executor, _) = Build(serial: "9f8e7d6c5b4a3210");

        var result = await executor.ExecuteAsync(Rename("workshop-tablet-3"), null, default);

        Assert.Contains("Settings", result.Output);
        Assert.Contains("workshop-tablet-3", result.Output);
        Assert.Contains("Pixel-9-abcd1234", result.Output); // and what it used to be
    }

    [Fact]
    public async Task Missing_new_name_is_a_failure_not_a_no_op()
    {
        var (executor, _) = Build(serial: "9f8e7d6c5b4a3210");

        var result = await executor.ExecuteAsync(Rename(null), null, default);

        Assert.False(result.Success);
        Assert.Contains("new_name", result.Output);
    }

    [Fact]
    public async Task An_unstorable_rename_is_reported_as_failed()
    {
        // See MachineNameProviderTests: adopting a name that is not on disk is what produces
        // the duplicate days later, so the command has to fail here.
        var store = new FakeStateStore { WritesFail = true };
        var (executor, names) = Build(serial: "9f8e7d6c5b4a3210", store: store);

        var result = await executor.ExecuteAsync(Rename("workshop-tablet-3"), null, default);

        Assert.False(result.Success);
        Assert.Equal("Pixel-9-abcd1234", names.Current);
    }

    [Fact]
    public void Type_matches_the_hubs_command_type_exactly()
    {
        // A type spelled differently routes nowhere, and the console still shows the button.
        var (executor, _) = Build(serial: "x");
        Assert.Equal("rename", executor.Type);
    }
}
