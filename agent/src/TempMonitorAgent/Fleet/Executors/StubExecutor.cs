namespace TempMonitorAgent.Fleet.Executors;

/// <summary>
/// Placeholder for command types not yet implemented (install_driver, update_bios).
/// The hub accepts and queues them and the dispatcher routes them, so the full channel
/// is exercised end-to-end; this just reports "not implemented" until the vendor tooling
/// (Dell DCU, etc.) is wired up in a later pass.
/// </summary>
public sealed class StubExecutor : ICommandExecutor
{
    public string Type { get; }

    public StubExecutor(string type) => Type = type;

    public Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
        => Task.FromResult(CommandResult.Fail($"{Type} is not implemented yet"));
}
