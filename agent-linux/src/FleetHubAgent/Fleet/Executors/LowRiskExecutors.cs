using Microsoft.Extensions.Logging;

namespace FleetHubAgent.Fleet.Executors;

// restart/shutdown/rename take onOutput but ignore it: they return in well under the
// console's poll interval, so there is no progress to narrate.

/// <summary>
/// The shared shape of restart and shutdown.
///
/// **shutdown(8) schedules in MINUTES, and the hub sends seconds.** There is no time spec
/// that means "+90 seconds": the argument is either `now`, `+m` minutes, or an absolute
/// hh:mm. So a delay is rounded UP to the next whole minute and the result says what actually
/// got scheduled, because an operator who asked for 90 seconds and was told "scheduled" is
/// owed the real number.
///
/// Rejected alternative: sleep in-process for the exact delay and then call `systemctl
/// reboot`. That is precise and worse in every other way -- the delay would be invisible to
/// `shutdown -c`, no wall message would reach anyone signed in, a service restart in the
/// meantime would silently cancel it, and the executor would sit occupying one of four
/// concurrent command slots for up to a day.
/// </summary>
internal static class ShutdownScheduling
{
    /// <summary>The shutdown(8) time spec for a delay in seconds, plus how it reads to a
    /// human. `now` for anything non-positive, otherwise the next whole minute up.</summary>
    internal static (string Spec, string Human) TimeSpec(int delaySeconds)
    {
        if (delaySeconds <= 0) return ("now", "immediately");
        var minutes = (delaySeconds + 59) / 60;
        return ("+" + minutes.ToString(System.Globalization.CultureInfo.InvariantCulture),
                minutes == 1 ? "in 1 minute" : $"in {minutes} minutes");
    }

    /// <summary>What to append to the result when the operator asked for force.
    ///
    /// **`force` has no Linux counterpart and is deliberately not emulated.** On Windows it
    /// adds /f because an application with unsaved work can VETO a shutdown after shutdown.exe
    /// has already exited 0 -- the hub is told the restart was scheduled and the machine never
    /// goes down. Nothing on Linux can veto systemd like that: units get SIGTERM, then SIGKILL
    /// at the stop timeout, whether or not anyone asked for force. The one thing that WOULD
    /// change behaviour is `systemctl --force`, which skips unmounting filesystems -- that is
    /// data loss, not forcing, and no operator ticking a checkbox on a fleet console is asking
    /// for it. So the flag is honoured by saying, in the result, that it did nothing.</summary>
    internal static string ForceNote(bool force) =>
        force ? " (force requested; it has no effect on Linux -- see ShutdownScheduling)" : "";

    internal static async Task<CommandResult> RunAsync(
        string flag, string verb, FleetCommand cmd, CancellationToken ct)
    {
        var delay = cmd.Params.GetInt("delay_seconds", 60);
        var force = cmd.Params.GetBool("force");
        var (spec, human) = TimeSpec(delay);

        // The wall message is the only thing anyone signed in at the machine will see, so it
        // names the fleet rather than the command -- "FleetHub" is answerable, "shutdown" is
        // not.
        var run = await ProcessRunner.RunAsync(
            ProcessRunner.ProgramPath("/sbin/shutdown", "/usr/sbin/shutdown", "/usr/bin/shutdown"),
            new[] { flag, spec, $"FleetHub fleet {verb}" },
            timeoutSeconds: 30, onOutput: null, ct);

        return run.Succeeded
            ? CommandResult.Ok($"{verb} scheduled {human}{ShutdownScheduling.ForceNote(force)}")
            : CommandResult.Fail($"shutdown {flag} exited {run.ExitCode}: {run.Output}");
    }
}

/// <summary>restart: reboot the machine after an optional delay (default 60s).</summary>
public sealed class RestartExecutor : ICommandExecutor
{
    public string Type => "restart";

    public Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput, CancellationToken ct) =>
        ShutdownScheduling.RunAsync("-r", "restart", cmd, ct);
}

/// <summary>shutdown: power off the machine after an optional delay (default 60s).</summary>
public sealed class ShutdownExecutor : ICommandExecutor
{
    public string Type => "shutdown";

    public Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput, CancellationToken ct) =>
        ShutdownScheduling.RunAsync("-h", "shutdown", cmd, ct);
}

/// <summary>
/// rename: change the machine's hostname.
///
/// Unlike Windows, this takes effect IMMEDIATELY -- there is no reboot in between -- and that
/// is the interesting part, because the hub keys a machine on the name it reports. The next
/// /api/report after this lands arrives under a name the hub has never seen, and the console
/// grows a second machine.
///
/// **What collapses the two is the BIOS serial**, not anything this executor does: the hub
/// runs resolve_serial_group() on every report and merges an offline duplicate reporting the
/// same serial_number into the online one (see app.py, the OpenClaw -> OPENCLAW case). So the
/// rename is only safe on a machine whose DMI serial this agent can actually read -- see
/// SystemInfo, which needs root for /sys/class/dmi/id/product_serial. A VM with no serial set
/// renames into a permanent duplicate.
///
/// hostnamectl rather than writing /etc/hostname: it sets the transient (kernel) and static
/// names together, which is what makes the change visible to this process's own
/// Environment.MachineName without a restart.
/// </summary>
public sealed class RenameExecutor : ICommandExecutor
{
    private readonly ILogger<RenameExecutor> _log;
    public RenameExecutor(ILogger<RenameExecutor> log) => _log = log;

    public string Type => "rename";

    public async Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        var newName = cmd.Params.GetString("new_name");
        if (string.IsNullOrWhiteSpace(newName))
            return CommandResult.Fail("rename requires params.new_name");

        // Validated here rather than trusted from the hub. hostnamectl rejects a bad name
        // itself, but its message ("Invalid argument") does not say which character was the
        // problem, and this is a command whose failure an operator reads hours later.
        if (!IsValidHostname(newName))
            return CommandResult.Fail(
                $"'{newName}' is not a valid hostname: use 1-63 letters, digits and hyphens, " +
                "not starting or ending with a hyphen");

        var run = await ProcessRunner.RunAsync(
            ProcessRunner.ProgramPath("/usr/bin/hostnamectl", "/bin/hostnamectl"),
            new[] { "set-hostname", newName },
            timeoutSeconds: 30, onOutput: null, ct);

        if (!run.Succeeded)
        {
            _log.LogWarning("rename to {Name} failed: {Output}", newName, run.Output);
            return CommandResult.Fail($"hostnamectl exited {run.ExitCode}: {run.Output}");
        }

        // /etc/hosts is deliberately left alone. Debian-family images carry a 127.0.1.1 line
        // holding the old name, and rewriting a file the local admin owns -- one that may
        // carry hand-maintained entries -- is not something a fleet console should do behind
        // their back. Said out loud in the result so it is not a surprise later.
        return CommandResult.Ok(
            $"hostname is now '{newName}'. /etc/hosts was not modified; a 127.0.1.1 entry " +
            "naming the old hostname may need updating by hand.");
    }

    /// <summary>RFC 1123 label rules, which is what hostnamectl enforces. Internal so a test
    /// can hold the boundary cases without starting a process.</summary>
    internal static bool IsValidHostname(string name)
    {
        if (name.Length is 0 or > 63) return false;
        if (name[0] == '-' || name[^1] == '-') return false;
        foreach (var c in name)
            if (!char.IsAsciiLetterOrDigit(c) && c != '-') return false;
        return true;
    }
}
