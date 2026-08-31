using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging;
using TempMonitorAgent.Fleet;
using TempMonitorAgent.Fleet.Executors;

namespace TempMonitorAgent.Patch;

/// <summary>
/// The <c>install_patches</c> command (roadmap #14): install the updates the hub named, then
/// restart if the policy says to.
///
/// <para><b>This executor reports STAGED, never INSTALLED.</b> Its success result means "the
/// install APIs ran and did not refuse"; whether the updates actually applied is settled later
/// by the hub, which watches for them to stop being offered
/// (hub/patches.py confirm_from_inventory). That split is the whole reason patching is not a
/// package: a silent installer exiting 0 having done nothing is the normal failure mode here,
/// and Windows Update genuinely reports success for updates that need a restart before they
/// are installed in any sense that matters.</para>
///
/// <para><b>It re-scans before returning, and asks for the restart last.</b> The scan is what
/// makes the next heartbeat carry the truth (PatchInventoryReporter.Invalidate), and doing it
/// before the restart means a machine that reboots promptly still leaves a fresh report
/// behind. The restart is requested through the same delayed shutdown the `restart` command
/// uses, so a person sitting at the PC gets Windows' own warning rather than losing their work
/// to a patch window.</para>
///
/// <para><b>A run that installed nothing is still a successful command.</b> The hub asked this
/// machine to install a set it resolved from a report that may be minutes old; an update that
/// has since gone is not a failure of this command, and reporting one would spend a retry on a
/// machine with nothing wrong with it.</para>
/// </summary>
public sealed class InstallPatchesExecutor(ILogger<InstallPatchesExecutor> log) : ICommandExecutor
{
    /// <summary>Matches hub/patches.py COMMAND_TYPE.</summary>
    public string Type => "install_patches";

    /// <summary>How long the person at the PC gets before a patch restart takes it. Longer
    /// than the `restart` command's default minute: that one is an operator acting on a machine
    /// they are usually already talking to somebody about, while this fires inside a
    /// maintenance window with nobody watching.</summary>
    private const int RestartDelaySeconds = 300;

    public async Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput,
                                                  CancellationToken ct)
    {
        var uids = ReadUids(cmd.Params);
        if (uids.Count == 0)
        {
            return CommandResult.Fail("install_patches was given no updates to install.");
        }
        var policy = cmd.Params.GetString("reboot_policy") ?? "if_required";

        PatchInstaller.Outcome outcome;
        try
        {
            outcome = await Task.Run(() => PatchInstaller.Install(uids, onOutput, ct), ct);
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (Exception e)
        {
            log.LogWarning(e, "Patch install failed");
            return CommandResult.Fail($"The patch install could not be run: {e.Message}");
        }

        // Make the next heartbeat carry a fresh list whatever happens next. This is the report
        // the hub uses to close the run out, so it matters more than the result below.
        PatchInventoryReporter.Invalidate();

        var lines = new List<string>();
        if (outcome.Output.Length > 0) lines.Add(outcome.Output);

        if (outcome.Attempted.Count == 0)
        {
            lines.Add("Nothing was left to install on this machine.");
            return CommandResult.Ok(string.Join(Environment.NewLine, lines));
        }

        var shouldRestart = ShouldRestart(policy, outcome.RebootRequired);

        if (shouldRestart)
        {
            lines.Add($"Restarting in {RestartDelaySeconds / 60} minutes to finish the install.");
            onOutput?.Invoke(lines[^1]);
            // The same shutdown.exe call RestartExecutor makes, and deliberately NOT forced:
            // /f closes applications without giving them a chance to save, which is the right
            // trade when an operator is on the phone about a stuck machine and the wrong one
            // at 2am on a PC somebody left a document open on. The delay does the work here.
            var restart = await ProcessRunner.RunAsync(
                "shutdown.exe",
                $"/r /t {RestartDelaySeconds} /c \"FleetHub patch install\"",
                ct, timeoutSeconds: 30);
            if (restart.ExitCode != 0)
            {
                // The updates are staged either way; a restart that could not be scheduled is
                // worth saying out loud, because the hub will otherwise wait out its confirm
                // timeout on a machine nobody is going to reboot.
                log.LogWarning("Could not schedule the patch restart: shutdown /r exited {Code}",
                               restart.ExitCode);
                lines.Add($"The restart could not be scheduled (shutdown /r exited " +
                          $"{restart.ExitCode}). These updates finish installing at the " +
                          $"machine's next restart.");
            }
        }
        else if (outcome.RebootRequired)
        {
            lines.Add("A restart is required to finish, and the window's policy is not to " +
                      "restart. These updates finish installing at the machine's next restart.");
        }

        return CommandResult.Ok(string.Join(Environment.NewLine, lines));
    }

    /// <summary>Whether to restart, given the window's policy and what the install reported.
    ///
    /// <para>Extracted so it can be tested: this is the decision that finishes an install, and
    /// getting it wrong is silent in both directions. Deciding NOT to restart when one is owed
    /// leaves updates staged forever and the hub waiting out its confirm timeout on a machine
    /// that was never going to come back; deciding TO restart when none is needed reboots
    /// somebody's PC for nothing.</para>
    ///
    /// <para>An unrecognised policy falls through to <c>if_required</c> rather than to always
    /// or never, matching the hub's own default -- a garbled value must not silently turn a
    /// fleet's restarts on or off.</para>
    /// </summary>
    internal static bool ShouldRestart(string? policy, bool rebootRequired) =>
        policy?.Trim().ToLowerInvariant() switch
        {
            "always" => true,
            "never" => false,
            _ => rebootRequired,
        };

    /// <summary>The `uids` array from the command params. Tolerant of a malformed entry for the
    /// same reason the hub's parse_report is: one bad element must not cost the other twelve.</summary>
    private static List<string> ReadUids(JsonNode? paramsNode)
    {
        var uids = new List<string>();
        var array = paramsNode.GetArray("uids");
        if (array is null) return uids;
        foreach (var item in array)
        {
            if (item is null) continue;
            var text = item.ToString().Trim();
            if (text.Length > 0) uids.Add(text);
        }
        return uids;
    }
}
