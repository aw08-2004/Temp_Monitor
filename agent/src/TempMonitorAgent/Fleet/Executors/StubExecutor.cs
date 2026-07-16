namespace TempMonitorAgent.Fleet.Executors;

/// <summary>
/// Placeholder for high-risk command types not yet implemented (install_driver,
/// update_bios). The dispatcher still verifies their signature first, so this proves
/// the full signed path end-to-end; it just reports "not implemented" until the
/// vendor tooling (Dell DCU, etc.) is wired up in a later pass.
/// </summary>
public sealed class StubExecutor : ICommandExecutor
{
    public string Type { get; }

    public StubExecutor(string type) => Type = type;

    public Task<CommandResult> ExecuteAsync(FleetCommand cmd, CancellationToken ct)
        => Task.FromResult(CommandResult.Fail($"{Type} is not implemented yet"));
}
