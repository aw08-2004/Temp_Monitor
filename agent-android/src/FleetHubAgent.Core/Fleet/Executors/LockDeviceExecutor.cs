using Microsoft.Extensions.Logging;

namespace FleetHubAgent.Fleet.Executors;

/// <summary>
/// lock_device: lock the screen now -- roadmap #23 phase H.
///
/// **The cheap half of the pair, and it is meant to be.** A lock is the ordinary first move for
/// a phone left in a taxi: it costs the person holding the device one PIN entry to undo, so the
/// hub asks for no typed confirmation and this asks for no parameters. All the friction in this
/// feature belongs on the action that cannot be undone -- spreading it over both is how people
/// learn to type past it.
///
/// **"This device is not fully managed" is a FAIL, not a success with a note.** The opposite
/// call from LocateDeviceExecutor's, and the difference is what an operator does next. A device
/// that cannot give a position has answered the question truthfully and there is nothing to do
/// about it; a device that cannot lock has not done the thing that was asked, and somebody
/// hoping a lost phone is now locked must not read "done".
/// </summary>
public sealed class LockDeviceExecutor(
    ILogger<LockDeviceExecutor> log, IDeviceSecurity security) : ICommandExecutor
{
    /// <summary>Matches hub/wipe.py's COMMAND_FOR[ACTION_LOCK].</summary>
    public string Type => "lock_device";

    public Task<CommandResult> ExecuteAsync(
        FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        if (!security.CanEnforce)
        {
            log.LogWarning("Refused lock_device: this device is not a device owner");
            return Task.FromResult(CommandResult.Fail(
                "This device is not fully managed, so its screen cannot be locked remotely. " +
                "It has to be provisioned as a device owner, which needs a factory reset."));
        }

        bool locked;
        try
        {
            locked = security.Lock();
        }
        catch (Exception e)
        {
            // The contract says Lock does not throw; if it does, that is answered rather than
            // left to take down the command loop.
            log.LogWarning(e, "lock_device threw");
            return Task.FromResult(CommandResult.Fail($"Locking the screen failed: {e.Message}"));
        }

        if (!locked)
        {
            return Task.FromResult(CommandResult.Fail(
                "The device refused to lock its screen. Nothing has changed on it."));
        }

        log.LogInformation("Screen locked at the request of {Actor}",
            string.IsNullOrEmpty(cmd.IssuedBy) ? "the hub" : cmd.IssuedBy);
        return Task.FromResult(CommandResult.Ok("The screen is locked."));
    }
}
