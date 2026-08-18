using System.Text;
using TempMonitorAgent.Fleet.Shell;

namespace TempMonitorAgent.Fleet.Executors;

/// <summary>
/// run_script: run params.script in the issuing operator's PERSISTENT shell (params.shell =
/// "powershell" default, or "cmd"), so `cd`, environment and variables carry over to the next
/// submission — a real terminal on the box, not a fresh process each time. params.timeout_seconds
/// overrides the default per-submission timeout; on timeout the shell's children are killed but
/// the session is kept.
///
/// This runs arbitrary code as SYSTEM on the strength of the hub having authorized it (an
/// allow-listed console session); the hub's audit_log is the record of who asked for what. The
/// operator's identity (cmd.IssuedBy) comes from that trusted session and is what keys the shell,
/// so one operator can never drive another's session.
///
/// <b>A RULE gets a fresh shell every time; a person does not.</b> Persistence is the right
/// behaviour for someone typing at the Terminal tab — they expect their `cd` to still be there.
/// It is the wrong behaviour for a rule, which the hub issues as <c>rule:&lt;id&gt;</c>: every fire
/// of that rule on this machine would otherwise land in ONE PowerShell process, reused for as
/// long as the session stays warm, still carrying the previous fire's working directory,
/// variables and imported modules. Fire N would then not be reproducible from its own text, and
/// a script that happened to `cd` somewhere would silently change what the next fire's relative
/// paths meant. Nobody is watching to notice.
///
/// So an unattended submission is reset after it completes. The cost is one process start per
/// fire, on a schedule measured in minutes at worst, which buys every fire the same starting
/// state. See <see cref="IsUnattended"/>.
/// </summary>
public sealed class RunScriptExecutor : ICommandExecutor
{
    private readonly ShellSessionManager _sessions;

    public RunScriptExecutor(ShellSessionManager sessions) => _sessions = sessions;

    public string Type => "run_script";

    /// <summary>Was this issued by machinery rather than by a person at a console?
    ///
    /// The hub stamps <c>issued_by</c> from its own trusted session: an operator's email
    /// address, or a `rule:&lt;id&gt;` / `rules:probe` marker. Matching on the prefix is not
    /// elegant, but the alternative is a new wire field the hub would have to send and every
    /// older agent would ignore — and an older agent silently keeping the old, wrong behaviour
    /// is exactly what this is fixing. An email address cannot contain a colon before its
    /// local part, so there is no address this can mistake for a rule.</summary>
    private static bool IsUnattended(string issuedBy) =>
        issuedBy.StartsWith("rule:", StringComparison.OrdinalIgnoreCase)
        || issuedBy.StartsWith("rules:", StringComparison.OrdinalIgnoreCase);

    public async Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        var script = cmd.Params.GetString("script");
        if (string.IsNullOrEmpty(script))
            return CommandResult.Fail("run_script requires params.script");

        var shell = (cmd.Params.GetString("shell") ?? "powershell").ToLowerInvariant();
        var timeout = cmd.Params.GetInt("timeout_seconds", AgentConfig.ShellDefaultTimeoutSeconds);
        timeout = Math.Clamp(timeout, 1, 24 * 60 * 60);
        var email = string.IsNullOrEmpty(cmd.IssuedBy) ? "unknown" : cmd.IssuedBy;
        var unattended = IsUnattended(email);

        // Accumulate the full text for the durable result (command_results.output) while also
        // streaming it live. The dispatcher caps the returned result; the live stream has the
        // hub's larger per-command cap.
        var sb = new StringBuilder();
        void Sink(string text) { sb.Append(text); onOutput?.Invoke(text); }

        try
        {
            var session = await _sessions.GetOrCreateAsync(email, shell, ct);
            var outcome = await session.RunAsync(script, timeout, Sink, ct);

            // Nobody is at the keyboard, so nothing should carry over to the next fire.
            // Deliberately AFTER the submission rather than before it: resetting first would
            // leave the shell alive between fires for the idle reaper to collect, and a
            // concurrent submission from the same rule would still meet the old process.
            if (unattended)
                await _sessions.ResetAsync(email, shell);

            if (outcome.ShellDied)
            {
                await _sessions.ResetAsync(email, shell);
                sb.Append("\n[agent] the shell session ended; it has been reset — rerun your command.\n");
                return new CommandResult(false, sb.ToString(), outcome.Cwd);
            }
            if (outcome.TimedOut)
                sb.Append($"\n[agent] timed out after {timeout}s; child processes were killed, session kept.\n");

            var success = outcome.ExitCode == 0 && !outcome.TimedOut;
            return new CommandResult(success, sb.ToString(), outcome.Cwd);
        }
        catch (Exception e)
        {
            return CommandResult.Fail($"run_script error: {e.Message}");
        }
    }
}
