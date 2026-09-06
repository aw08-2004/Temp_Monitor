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
/// **An unimplemented command answers, and says why.** The Windows agent can leave a type
/// unregistered because it implements nearly all of them; this agent implements four out of
/// roughly thirty, so "unknown command type" would be the usual answer and would read as a
/// bug in the hub. The message below names the platform instead, so an operator who queued a
/// remote session against a Linux box learns that in the result rather than by guessing.
/// (The console should not have offered it -- every one of those features is gated behind a
/// MIN_*_AGENT version this agent sits below, see AgentConfig.Version -- so reaching this
/// path at all is worth the clearer message.)
/// </summary>
public sealed class CommandDispatcher
{
    /// <summary>Matches the Windows dispatcher's cap, which in turn matches what the hub will
    /// store for one command's result.</summary>
    private const int MaxOutputChars = 16_000;

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
            return CommandResult.Fail(
                $"'{cmd.Type}' is not implemented by the Linux agent " +
                $"(v{AgentConfig.Version}). Implemented: " +
                string.Join(", ", _executors.Keys.Order(StringComparer.Ordinal)) + ".");
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

    private static CommandResult Truncate(CommandResult r)
    {
        if (r.Output is { Length: > MaxOutputChars } o)
            return r with { Output = o[..MaxOutputChars] + "\n…(truncated)" };
        return r;
    }
}
