using Microsoft.Extensions.Logging;
using TempMonitorAgent.Fleet.Shell;

namespace TempMonitorAgent.Fleet.Executors;

/// <summary>
/// shell_open: attach a real pseudoconsole to the terminal session the hub has already
/// created, and stay attached until it ends.
///
/// This is the only LONG-LIVED command the agent has, and it is long-lived on purpose. The
/// command is the session's lifetime: it is claimed when the operator opens the Terminal
/// tab and it completes when the shell exits, so the audit trail reads "this operator held
/// a SYSTEM console on this machine from X to Y" -- which is exactly the fact worth
/// recording about a terminal. What was TYPED is not recorded, here or on the hub; that is
/// a live stream in a rolling buffer (hub/terminal.py), not a transcript.
///
/// It carries no output through the command channel either. `onOutput` is ignored and the
/// result text is a one-line summary, because the VT stream goes over the pty endpoints on
/// its own cadence -- see PtySessionRunner for why the command channel's tick is unusable
/// for a terminal.
///
/// Note for the Worker: this must be EXEMPT from MaxConcurrentCommands. It occupies its
/// slot for as long as an operator keeps a tab open, so counting it would let four idle
/// terminals block every restart, script and deployment on the machine.
/// </summary>
public sealed class ShellOpenExecutor : ICommandExecutor
{
    private readonly PtySessionManager _sessions;
    private readonly IPtyChannel _channel;
    private readonly ILogger<ShellOpenExecutor> _log;

    public ShellOpenExecutor(PtySessionManager sessions, IPtyChannel channel,
                             ILogger<ShellOpenExecutor> log)
    {
        _sessions = sessions;
        _channel = channel;
        _log = log;
    }

    public string Type => "shell_open";

    public async Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        var sessionId = cmd.Params.GetString("session_id");
        if (string.IsNullOrWhiteSpace(sessionId))
            return CommandResult.Fail("shell_open requires params.session_id");

        var shell = (cmd.Params.GetString("shell") ?? "powershell").ToLowerInvariant();
        var cols = (short)Math.Clamp(cmd.Params.GetInt("cols", 120), 20, 500);
        var rows = (short)Math.Clamp(cmd.Params.GetInt("rows", 30), 5, 200);

        var runner = new PtySessionRunner(sessionId, _channel, _log);
        if (!_sessions.TryAdd(runner))
        {
            // Tell the hub as well as the command: the console is polling the session, not
            // this command's result, so a failure reported only here would look like a
            // terminal that opened and then said nothing.
            const string reason = "too many terminals are already open on this machine";
            await _channel.ReportPtyClosedAsync(sessionId, reason, CancellationToken.None);
            return CommandResult.Fail(reason);
        }

        _log.LogInformation("Opening terminal {Id} ({Shell} {Cols}x{Rows}) for {Op}",
            sessionId, shell, cols, rows, OperatorTag.For(cmd.IssuedBy));

        try
        {
            var reason = await runner.RunAsync(shell, cols, rows, ct);
            // Ending is the normal case, not a failure: `exit` is how you close a terminal.
            return CommandResult.Ok($"terminal session ended: {reason}");
        }
        finally
        {
            _sessions.Remove(sessionId);
            runner.Dispose();
        }
    }
}
