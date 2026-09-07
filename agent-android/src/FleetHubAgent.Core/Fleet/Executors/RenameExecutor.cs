using Microsoft.Extensions.Logging;

namespace FleetHubAgent.Fleet.Executors;

/// <summary>
/// rename: change the name this device reports to the hub.
///
/// **It is not a hostname change, and the result says so.** The Windows agent renames the
/// computer; the Linux agent calls hostnamectl. Android has no hostname an app may set -- so
/// what this changes is the name the agent SENDS, stored on the device. The distinction
/// matters to an operator because nothing else on the phone changes: Settings still shows the
/// old device name, and so does anything else on the network. Saying "renamed" and leaving
/// them to discover that is how a console earns a reputation for lying.
///
/// **The dangerous part is the same as on Linux, and for the same reason.** The hub keys a
/// machine on the name it reports, so the next /api/report after this lands arrives under a
/// name the hub has never seen, and the console grows a second machine. What collapses the two
/// is resolve_serial_group(), which merges an offline duplicate reporting the same
/// serial_number into the online one -- so a rename is only safe on a device whose
/// serial_number this agent can actually report. On Android that field carries the SSAID
/// rather than a hardware serial (see AndroidSystemInfo, which explains why, and which known
/// bad value is filtered), and a device that could not read one renames itself into a
/// permanent duplicate.
///
/// That is a real possibility rather than a theoretical one, so it is checked here: with no
/// usable serial the rename is REFUSED, with an explanation, rather than performed into a
/// duplicate. Rejected alternative: renaming anyway and warning in the result. An operator
/// reads a result once, and the duplicate outlives the reading.
/// </summary>
public sealed class RenameExecutor(
    ILogger<RenameExecutor> log,
    MachineNameProvider names,
    SystemIdentity identity) : ICommandExecutor
{
    public string Type => "rename";

    public Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        var newName = cmd.Params.GetString("new_name");
        if (string.IsNullOrWhiteSpace(newName))
            return Task.FromResult(CommandResult.Fail("rename requires params.new_name"));

        if (string.IsNullOrEmpty(identity.SerialNumber))
        {
            log.LogWarning("Refused rename to {Name}: no stable id to collapse the duplicate on", newName);
            return Task.FromResult(CommandResult.Fail(
                "This device reports no stable id, so the hub could not merge the machine it " +
                "is about to become with the one it is now -- the rename would leave two " +
                "entries in the console. Reinstall the agent, or grant it the permission it " +
                "needs to read one, then try again."));
        }

        var previous = names.Current;
        if (!names.TrySet(newName!, out var error))
        {
            log.LogWarning("rename to {Name} failed: {Error}", newName, error);
            return Task.FromResult(CommandResult.Fail(error));
        }

        log.LogInformation("Machine name is now {Name} (was {Previous})", names.Current, previous);

        // Named in full because this is the one command whose effect an operator will look for
        // in the wrong place. The delay is worth stating too: the next report is up to
        // AgentConfig.IntervalSeconds away, so the console does not change the instant this
        // result arrives.
        return Task.FromResult(CommandResult.Ok(
            $"This device now reports as '{names.Current}' (was '{previous}'). Android has no " +
            "hostname an app may set, so the device's own name in Settings is unchanged -- " +
            "only what it tells the hub. The console will catch up on the next report."));
    }
}
