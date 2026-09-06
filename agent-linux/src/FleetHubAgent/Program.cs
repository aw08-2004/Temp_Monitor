using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Serilog;
using FleetHubAgent;
using FleetHubAgent.Fleet;
using FleetHubAgent.Fleet.Executors;
using FleetHubAgent.State;
using FleetHubAgent.Telemetry;
using FleetHubAgent.Update;

// Composition root, and nothing else -- the same rule the hub's app.py follows.
//
// It is much shorter than the Windows agent's Program.cs, and the difference is almost
// entirely the branches that are missing: that binary re-invokes ITSELF as a session-injected
// helper (--remote-helper, --show-message) because Windows session 0 has no desktop to draw
// on. Linux has no session-0 problem to solve and no desktop features here to solve it for, so
// this starts a host and nothing more.

// stdout only. systemd journals it, rotates it and expires it; a second copy under /var/log
// would be one more thing that can fill a disk. The template matches the Windows agent's file
// log so a line reads the same in either place.
Log.Logger = new LoggerConfiguration()
    .MinimumLevel.Information()
    .WriteTo.Console(
        outputTemplate: "{Timestamp:yyyy-MM-dd HH:mm:ss} {Level:u3} {Message:lj}{NewLine}{Exception}")
    .CreateLogger();

try
{
    var builder = Host.CreateApplicationBuilder(args);

    // Type=notify: systemd learns the agent is UP when the host has actually started, not when
    // the process was forked. Without it a unit ordered after this one starts against an agent
    // that has not read its config yet.
    builder.Services.AddSystemd();
    builder.Services.AddSerilog();

    // Core state + telemetry
    builder.Services.AddSingleton<AgentState>();
    builder.Services.AddSingleton<ISensorSource, ProcSensorReader>();
    builder.Services.AddSingleton(sp =>
        SystemInfo.Read(sp.GetRequiredService<ILoggerFactory>().CreateLogger("SystemInfo")));
    builder.Services.AddSingleton<TelemetryReporter>();

    // Fleet command channel
    builder.Services.AddSingleton<FleetClient>();
    builder.Services.AddSingleton<CommandDispatcher>();

    // The executors, which are the whole of what this agent can be TOLD to do today. Four out
    // of the hub's ~thirty command types, and the gap is deliberate rather than unfinished:
    // every other type backs a feature the console gates behind a MIN_*_AGENT version this
    // agent sits below (see AgentConfig.Version), so an operator is never offered one. Anything
    // that arrives anyway gets a result naming the platform -- see CommandDispatcher.
    builder.Services.AddSingleton<ICommandExecutor, RestartExecutor>();
    builder.Services.AddSingleton<ICommandExecutor, ShutdownExecutor>();
    builder.Services.AddSingleton<ICommandExecutor, RenameExecutor>();
    builder.Services.AddSingleton<ICommandExecutor, RunScriptExecutor>();

    // Signed self-update. The trust root is an Ed25519 key held offline, not the hub -- see
    // SelfUpdater and AgentConfig.UpdatePublicKeyHex.
    builder.Services.AddSingleton<SelfUpdater>();

    builder.Services.AddHostedService<Worker>();

    var host = builder.Build();
    host.Run();
}
catch (Exception ex)
{
    Log.Fatal(ex, "Agent terminated unexpectedly");
}
finally
{
    Log.CloseAndFlush();
}

return 0;
