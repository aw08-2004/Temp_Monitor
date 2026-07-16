using Microsoft.Extensions.Logging;
using TempMonitorAgent.Fleet.Executors;

namespace TempMonitorAgent.Fleet;

/// <summary>
/// Routes a claimed command to its executor. Enforces the risk tier using the AGENT's
/// own classification — not the hub's requires_signature flag — so a compromised hub
/// cannot downgrade a high-risk command by clearing the flag. High-risk types must
/// carry a valid offline Ed25519 signature over the canonical payload or they are
/// refused before any code runs (the second of the channel's two gates).
/// </summary>
public sealed class CommandDispatcher
{
    // Must match fleet.py HIGH_RISK_COMMANDS. The agent trusts this set, not the hub.
    private static readonly HashSet<string> HighRisk = new()
    {
        "run_script", "install_driver", "update_bios",
    };

    private const int MaxOutputChars = 16_000;

    private readonly ILogger<CommandDispatcher> _log;
    private readonly Dictionary<string, ICommandExecutor> _executors;

    public CommandDispatcher(ILogger<CommandDispatcher> log, IEnumerable<ICommandExecutor> executors)
    {
        _log = log;
        _executors = executors.ToDictionary(e => e.Type, StringComparer.Ordinal);
    }

    public async Task<CommandResult> ExecuteAsync(FleetCommand cmd, CancellationToken ct)
    {
        if (!_executors.TryGetValue(cmd.Type, out var executor))
            return CommandResult.Fail($"unknown command type: {cmd.Type}");

        // Agent-side gate: any high-risk type needs a valid signature, regardless of
        // what requires_signature says on the wire.
        if (HighRisk.Contains(cmd.Type))
        {
            var ok = SignatureVerifier.VerifyCommand(
                AgentConfig.CommandSigningPublicKeyHex,
                cmd.Type, AgentConfig.MachineName, cmd.Params, cmd.Signature);
            if (!ok)
            {
                _log.LogWarning("Refused {Type} {Id}: signature verification failed", cmd.Type, cmd.Id);
                return CommandResult.Fail("signature verification failed — command refused");
            }
        }

        try
        {
            _log.LogInformation("Executing {Type} {Id}", cmd.Type, cmd.Id);
            var result = await executor.ExecuteAsync(cmd, ct);
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
