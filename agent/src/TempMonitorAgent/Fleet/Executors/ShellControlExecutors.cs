using TempMonitorAgent.Fleet.Shell;

namespace TempMonitorAgent.Fleet.Executors;

// Session-control commands for the interactive terminal. Each addresses the ISSUING
// operator's shell (cmd.IssuedBy, set by the hub from the trusted session) and completes
// immediately -- the visible effect (a program unblocking, output stopping) streams back on
// the run_script command that is actually running in that shell.
//
// None of these create a session: if the operator has no live shell, there's nothing to
// steer, so they report a benign failure rather than spinning one up.

/// <summary>shell_input: write params.data straight to the running submission's stdin, e.g.
/// answering a "Continue? [Y/N]" prompt.</summary>
public sealed class ShellInputExecutor : ICommandExecutor
{
    private readonly ShellSessionManager _sessions;
    public ShellInputExecutor(ShellSessionManager sessions) => _sessions = sessions;
    public string Type => "shell_input";

    public Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        var data = cmd.Params.GetString("data") ?? "";
        var shell = cmd.Params.GetString("shell") ?? "powershell";
        var session = _sessions.TryGet(EmailOf(cmd), shell);
        if (session is null)
            return Task.FromResult(CommandResult.Fail("no active shell session"));
        session.WriteInput(data);
        return Task.FromResult(CommandResult.Ok());
    }

    internal static string EmailOf(FleetCommand cmd) =>
        string.IsNullOrEmpty(cmd.IssuedBy) ? "unknown" : cmd.IssuedBy;
}

/// <summary>shell_signal: Ctrl-C equivalent -- kill the shell's child processes, keep the
/// shell (and its cwd/variables).</summary>
public sealed class ShellSignalExecutor : ICommandExecutor
{
    private readonly ShellSessionManager _sessions;
    public ShellSignalExecutor(ShellSessionManager sessions) => _sessions = sessions;
    public string Type => "shell_signal";

    public Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        var shell = cmd.Params.GetString("shell") ?? "powershell";
        var session = _sessions.TryGet(ShellInputExecutor.EmailOf(cmd), shell);
        if (session is null)
            return Task.FromResult(CommandResult.Fail("no active shell session"));
        session.SignalInterrupt();
        return Task.FromResult(CommandResult.Ok("interrupt sent"));
    }
}

/// <summary>shell_reset: dispose the operator's shell so the next run_script starts a fresh
/// one (back at the default working directory).</summary>
public sealed class ShellResetExecutor : ICommandExecutor
{
    private readonly ShellSessionManager _sessions;
    public ShellResetExecutor(ShellSessionManager sessions) => _sessions = sessions;
    public string Type => "shell_reset";

    public async Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        var shell = cmd.Params.GetString("shell") ?? "powershell";
        await _sessions.ResetAsync(ShellInputExecutor.EmailOf(cmd), shell);
        return CommandResult.Ok("session reset");
    }
}
