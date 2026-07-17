using Microsoft.Extensions.Logging;
using TempMonitorAgent.Fleet.Executors;

namespace TempMonitorAgent.Fleet;

/// <summary>
/// Routes a claimed command to its executor.
///
/// The agent no longer second-guesses the hub on authorization: commands used to carry
/// an offline Ed25519 signature that was verified here before execution, but that model
/// could not serve a helpdesk group and was never live in production (no key was ever
/// configured, so every high-risk command was refused outright). Authorization now lives
/// entirely at the hub's console session gate, and the hub's audit_log is the record.
/// Enrollment still bounds who can even receive commands, and the SEPARATE update trust
/// root (SignatureVerifier.VerifyRaw + SelfUpdater) still bounds what code may run.
/// </summary>
public sealed class CommandDispatcher
{
    private const int MaxOutputChars = 16_000;

    private readonly ILogger<CommandDispatcher> _log;
    private readonly Dictionary<string, ICommandExecutor> _executors;

    public CommandDispatcher(ILogger<CommandDispatcher> log, IEnumerable<ICommandExecutor> executors)
    {
        _log = log;
        _executors = executors.ToDictionary(e => e.Type, StringComparer.Ordinal);
    }

    /// <summary><paramref name="onOutput"/> is handed to the executor for live streaming;
    /// the returned CommandResult still carries the complete output regardless, so the
    /// hub's durable record doesn't depend on anyone watching.</summary>
    public async Task<CommandResult> ExecuteAsync(
        FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        if (!_executors.TryGetValue(cmd.Type, out var executor))
            return CommandResult.Fail($"unknown command type: {cmd.Type}");

        try
        {
            _log.LogInformation("Executing {Type} {Id}", cmd.Type, cmd.Id);
            var result = await executor.ExecuteAsync(cmd, onOutput, ct);
            return Truncate(result);
        }
        catch (Exception e)
        {
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
