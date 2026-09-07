using Microsoft.Extensions.Logging;
using FleetHubAgent.Fleet.Executors;

namespace FleetHubAgent.Fleet;

/// <summary>
/// Routes a claimed command to its executor.
///
/// Authorization is not re-checked here. Commands are not signed -- that model was removed
/// deliberately (see hub/fleet.py) -- and authorization lives entirely at the hub's console
/// session gate, with the hub's audit_log as the record. Enrollment bounds who can receive a
/// command at all.
///
/// **An unimplemented command answers, and says why it can never be implemented.** The Linux
/// agent's version of this message says a type is "not implemented", which is the truth there:
/// restart, run_script and the rest are all perfectly possible on Linux and simply are not
/// written yet. On Android most of them are not possible at all for an ordinary app -- there
/// is no API to reboot a device, no shell to run a script in, no way to enumerate another
/// app's processes -- so "not implemented" would send an operator to look for a version of
/// this agent that will do it, and there is not going to be one. The message therefore
/// distinguishes the two cases; see <see cref="Impossible"/>.
///
/// **Do not assume this path is rare.** The console's MIN_*_AGENT gates stop an operator being
/// offered a button, but they are JavaScript in the console and nothing runs them for a command
/// the hub's own scheduler or rules engine dispatches. A phone in a fleet-wide backup profile
/// gets backup_files queued to it on the profile's schedule, gate or no gate -- which is what
/// the first real device did, within a minute of enrolling. See the note on Impossible.
/// </summary>
public sealed class CommandDispatcher
{
    /// <summary>Matches the Windows and Linux dispatchers' cap, which in turn matches what the
    /// hub will store for one command's result.</summary>
    private const int MaxOutputChars = 16_000;

    /// <summary>
    /// Command types an ordinary Android app cannot execute at any version, and the reason,
    /// phrased for the operator reading the result rather than for a developer.
    ///
    /// **Every one of these is a platform limit, not a backlog item.** They are listed rather
    /// than lumped under one message because the follow-up differs: some have a real answer
    /// through an MDM or a device-owner deployment (reboot, app install), and some have none at
    /// all (running a script). An operator who is told which is which can act; one told
    /// "unsupported" opens a ticket.
    ///
    /// **The scheduler-driven entries are here because a real device received one.** An early
    /// version of this file assumed the console's MIN_*_AGENT gates meant an Android machine
    /// would never be OFFERED these, so reaching this path would be rare. That is true only of
    /// commands a person clicks. hub/fleet.py's SCHEDULED_COMMANDS -- backup_files,
    /// restore_files, install_patches, deploy_package -- are dispatched by the hub's scheduler
    /// and rules engine, which target a machine set rather than a console button, and those
    /// gates are console-side JavaScript that never runs for them. The first phone to enroll
    /// was in a fleet-wide backup profile and had backup_files queued to it within a minute.
    ///
    /// So this path is ORDINARY on Android, not exceptional, and the message it produces is
    /// read by an operator wondering why a scheduled job shows a failure against one machine
    /// every night. The real fix is on the hub: scope the profile so it does not target
    /// machines that cannot answer. Recorded in ROADMAP.MD #23.
    /// </summary>
    private static readonly Dictionary<string, string> Impossible = new(StringComparer.Ordinal)
    {
        ["backup_files"] =
            "An app cannot read another app's data or the device's filesystem, so there is " +
            "nothing here to back up that would mean what the console implies. Exclude " +
            "Android machines from the backup profile.",
        ["restore_files"] =
            "Nothing can be restored to a device this agent could not back up in the first " +
            "place -- see backup_files.",
        ["install_patches"] =
            "Android updates itself from the system updater, Play, or an MDM. An app cannot " +
            "install an OS update or patch another app.",
        ["deploy_package"] =
            "An app cannot install another app. That needs the package installer with the " +
            "user accepting each install, or an MDM holding device-owner rights.",
        ["install_app"] =
            "There is no winget equivalent an app may drive. Software reaches a managed " +
            "Android device through its MDM or the Play store.",
        ["gpupdate"] =
            "Group Policy is a Windows directory feature and has no Android counterpart. " +
            "Configuration reaches this agent through managed configuration instead.",
        ["restart"] =
            "Android has no reboot API for an ordinary app. A device enrolled as device-owner " +
            "through an MDM can be rebooted by that MDM.",
        ["shutdown"] =
            "Android has no power-off API for an ordinary app, and none for a device-owner " +
            "app either -- a phone is powered off by its holder.",
        ["run_script"] =
            "There is no shell for an app to run a script in. An app may only execute its own " +
            "code, inside its own sandbox, as its own unprivileged user.",
        ["list_directory"] =
            "An app can see its own storage and whatever the user has granted, not the " +
            "device's filesystem. There is nothing to browse that would mean what the file " +
            "explorer implies.",
        ["kill_process"] =
            "An app cannot see or end another app's processes; that has been true since " +
            "Android 8.",
        ["start_remote_session"] =
            "Screen capture on Android requires the person holding the device to accept a " +
            "system prompt for every session, so an unattended remote view is not possible.",
    };

    private readonly ILogger<CommandDispatcher> _log;
    private readonly Dictionary<string, ICommandExecutor> _executors;

    public CommandDispatcher(ILogger<CommandDispatcher> log, IEnumerable<ICommandExecutor> executors)
    {
        _log = log;
        _executors = executors.ToDictionary(e => e.Type, StringComparer.Ordinal);
    }

    public async Task<CommandResult> ExecuteAsync(
        FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        if (!_executors.TryGetValue(cmd.Type, out var executor))
        {
            _log.LogWarning("No executor for {Type} on this platform", cmd.Type);
            return CommandResult.Fail(Unsupported(cmd.Type, _executors.Keys));
        }

        try
        {
            _log.LogInformation("Executing {Type} {Id}", cmd.Type, cmd.Id);
            return Truncate(await executor.ExecuteAsync(cmd, onOutput, ct));
        }
        catch (Exception e)
        {
            // An executor that throws must still produce a RESULT. The hub shows a command
            // with no result as running forever, and the operator's only recourse is to wait
            // out a timeout that does not exist.
            _log.LogWarning(e, "Executor for {Type} threw", cmd.Type);
            return CommandResult.Fail($"executor error: {e.Message}");
        }
    }

    /// <summary>The message for a command this agent did not route. Internal so a test can
    /// hold the two shapes apart -- the distinction between "cannot" and "not yet" is the
    /// whole point of the table above, and it is easy to lose in a later edit.</summary>
    internal static string Unsupported(string type, IEnumerable<string> implemented)
    {
        var known = string.Join(", ", implemented.Order(StringComparer.Ordinal));
        if (Impossible.TryGetValue(type, out var why))
            return $"'{type}' cannot be run on Android. {why} Implemented here: {known}.";

        return $"'{type}' is not implemented by the Android agent (v{AgentConfig.Version}). " +
               $"Implemented: {known}.";
    }

    private static CommandResult Truncate(CommandResult r)
    {
        if (r.Output is { Length: > MaxOutputChars } o)
            return r with { Output = o[..MaxOutputChars] + "\n…(truncated)" };
        return r;
    }
}
