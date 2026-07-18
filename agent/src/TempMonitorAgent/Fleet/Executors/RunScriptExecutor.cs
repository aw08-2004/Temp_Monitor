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
/// </summary>
public sealed class RunScriptExecutor : ICommandExecutor
{
    private readonly ShellSessionManager _sessions;

    public RunScriptExecutor(ShellSessionManager sessions) => _sessions = sessions;

    public string Type => "run_script";

    public async Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        var script = cmd.Params.GetString("script");
        if (string.IsNullOrEmpty(script))
            return CommandResult.Fail("run_script requires params.script");

        var shell = (cmd.Params.GetString("shell") ?? "powershell").ToLowerInvariant();
        var timeout = cmd.Params.GetInt("timeout_seconds", AgentConfig.ShellDefaultTimeoutSeconds);
        timeout = Math.Clamp(timeout, 1, 24 * 60 * 60);
        var email = string.IsNullOrEmpty(cmd.IssuedBy) ? "unknown" : cmd.IssuedBy;

        // Accumulate the full text for the durable result (command_results.output) while also
        // streaming it live. The dispatcher caps the returned result; the live stream has the
        // hub's larger per-command cap.
        var sb = new StringBuilder();
        void Sink(string text) { sb.Append(text); onOutput?.Invoke(text); }

        try
        {
            var session = await _sessions.GetOrCreateAsync(email, shell, ct);
            var outcome = await session.RunAsync(script, timeout, Sink, ct);

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
