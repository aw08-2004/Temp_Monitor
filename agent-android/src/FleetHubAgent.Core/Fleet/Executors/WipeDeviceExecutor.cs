using Microsoft.Extensions.Logging;

namespace FleetHubAgent.Fleet.Executors;

/// <summary>
/// wipe_device: erase the device -- roadmap #23 phase H.
///
/// **The result is reported BEFORE the wipe happens, and that ordering is the whole executor.**
/// `wipeData` does not return: the process is killed, the data partition goes, and nothing on
/// this device ever speaks to the hub again. An executor that called it inline would be a
/// command that is claimed and never answered -- the console would show it running forever,
/// beside a machine that simply stopped reporting, which is indistinguishable from a flat
/// battery. So this answers "the wipe has started", the command loop posts that result the
/// moment this returns, and the erase fires a few seconds later.
///
/// The delay is the price of a truthful console and it is deliberately generous: a phone on a
/// slow connection needs time for one POST, and the cost of it being too short is the one
/// symptom this design exists to avoid. Nothing depends on the device surviving the delay --
/// if it is switched off in between, the command was never confirmed and the hub's own request
/// row still records that somebody asked.
///
/// **The hub is where a wipe is confirmed, not here.** wipe_web.py requires the machine's name
/// typed out and writes its audit row before the command exists. This executor deliberately
/// adds no second confirmation of its own: a device that second-guessed a wipe would be a
/// device that cannot be wiped when it matters, and the check belongs where a person is.
///
/// **No disclosure notification, unlike a locate.** A locate posts one because the person
/// holding the device has a right to know they were found and the device continues to exist to
/// show it. Here there is no "afterwards": the notification would be erased along with
/// everything else within seconds, and a warning that cannot be acted on is theatre.
/// </summary>
public sealed class WipeDeviceExecutor(
    ILogger<WipeDeviceExecutor> log, IDeviceSecurity security, TimeSpan? delay = null)
    : ICommandExecutor
{
    /// <summary>Matches hub/wipe.py's COMMAND_FOR[ACTION_WIPE].</summary>
    public string Type => "wipe_device";

    /// <summary>How long the device keeps existing after answering. Long enough for the
    /// command loop to POST the result over a slow connection; short enough that nobody is
    /// walking away with a device they believe is being erased and is not.</summary>
    internal static readonly TimeSpan DefaultDelay = TimeSpan.FromSeconds(10);

    /// <summary>Overridable ONLY so the tests can assert the ordering this class exists for
    /// without waiting ten seconds for it. Nothing in the app passes a value.</summary>
    private readonly TimeSpan _delay = delay ?? DefaultDelay;

    public Task<CommandResult> ExecuteAsync(
        FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        if (!security.CanEnforce)
        {
            log.LogWarning("Refused wipe_device: this device is not a device owner");
            return Task.FromResult(CommandResult.Fail(
                "This device is not fully managed, so it cannot be erased remotely. It has to " +
                "be provisioned as a device owner, which needs a factory reset."));
        }

        var clearResetProtection = cmd.Params.GetBool("reset_protection", true);
        log.LogWarning("WIPING THIS DEVICE in {Seconds}s at the request of {Actor}; " +
                       "factory-reset protection will be {Fate}",
            _delay.TotalSeconds,
            string.IsNullOrEmpty(cmd.IssuedBy) ? "the hub" : cmd.IssuedBy,
            clearResetProtection ? "cleared" : "left in place");

        // Fire and forget, on CancellationToken.None. Tying this to the command's token would
        // cancel the wipe if the service were stopping -- and a service stopping is exactly
        // what a wiped device looks like from the inside a moment later.
        _ = Task.Run(async () =>
        {
            try
            {
                await Task.Delay(_delay, CancellationToken.None);
                security.Wipe(clearResetProtection);
                // Only reached if the platform refused. Not a result anybody will read from
                // this device -- the command has already been answered -- so the log is the
                // only trace, and the hub's request row is what says a wipe was asked for.
                log.LogError("The wipe did not take effect. This device has NOT been erased.");
            }
            catch (Exception e)
            {
                log.LogError(e, "The wipe threw. This device has NOT been erased.");
            }
        }, CancellationToken.None);

        return Task.FromResult(CommandResult.Ok(
            clearResetProtection
                ? "The wipe has started. Factory-reset protection will be cleared, so the " +
                  "device can be set up again by anybody."
                : "The wipe has started. Factory-reset protection is being left in place, so " +
                  "the device cannot be set up again without the account that was on it."));
    }
}
