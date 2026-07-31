using TempMonitorAgent.Fleet.Executors;

namespace TempMonitorAgent.Bios;

/// <summary>
/// Re-reads the machine's firmware settings and lets the next heartbeat carry them
/// (roadmap #9).
///
/// The inventory rides the heartbeat on a six-hourly, change-detected cadence, which is right
/// for a fact that changes when a human walks up to a machine and wrong for the operator who
/// has just done exactly that. This is their refresh button.
/// </summary>
public sealed class RefreshBiosInventoryExecutor : ICommandExecutor
{
    public string Type => "refresh_bios_inventory";

    public Task<CommandResult> ExecuteAsync(
        FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        BiosInventoryReporter.Invalidate();
        BiosInventoryReporter.RefreshIfDue();
        // Returned inline as well, so the command result itself answers the question rather
        // than pointing at a tab that updates a heartbeat later. Costs nothing: the payload
        // has just been built.
        return Task.FromResult(CommandResult.Ok(BiosInventoryReporter.Build().ToJsonString()));
    }
}
