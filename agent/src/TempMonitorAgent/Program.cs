using Serilog;
using TempMonitorAgent;
using TempMonitorAgent.Backup;
using TempMonitorAgent.Fleet;
using TempMonitorAgent.Fleet.Executors;
using TempMonitorAgent.Fleet.Shell;
using TempMonitorAgent.Remote;
using TempMonitorAgent.State;
using TempMonitorAgent.Telemetry;
using TempMonitorAgent.Update;

// Remote view/control (roadmap #2): the service session-injects THIS SAME BINARY with
// --remote-helper <session-file> into the interactive desktop. That process is not a service
// -- it must not build the Windows Service host or start the telemetry loop -- so branch to
// the helper before anything else touches the service's logger or host.
if (RemoteHelper.TryGetSessionFileArg(args) is { } remoteSessionFile)
    return RemoteHelper.Run(remoteSessionFile);

// Standalone capture+encode self-test (roadmap #2, phase 2): writes an Annex-B .h264 file so
// the DXGI/GDI capture and H.264 encoder can be validated on real hardware with no hub.
if (RemoteHelper.TryGetCaptureTestArgs(args) is { } captureTestArgs)
    return RemoteHelper.RunCaptureSelfTest(captureTestArgs);

// Desktop-tracking diagnostic: prints the input desktop as it changes, and whether this
// process can actually attach to it. Everything about capturing the lock screen rests on that
// working, so it gets a way to be tested on its own before a full session is involved.
if (RemoteHelper.IsDesktopProbe(args))
    return RemoteHelper.RunDesktopProbe(args);

// Rotating file log under %ProgramData% so field issues on client machines are
// diagnosable (parity with companion.py's RotatingFileHandler). Console sink too,
// useful when run interactively for testing.
Directory.CreateDirectory(AgentConfig.ProgramDataDir);
Log.Logger = new LoggerConfiguration()
    .MinimumLevel.Information()
    .WriteTo.File(
        AgentConfig.LogPath,
        rollOnFileSizeLimit: true,
        fileSizeLimitBytes: 1_000_000,
        retainedFileCountLimit: 4,
        shared: true,
        outputTemplate: "{Timestamp:yyyy-MM-dd HH:mm:ss} {Level:u3} {Message:lj}{NewLine}{Exception}")
    .CreateLogger();

// Resolved during the ProgramDataDir touch above, before the logger existed.
if (AgentConfig.TakeMigrationNote() is { Length: > 0 } note) Log.Information("{Note}", note);

try
{
    var builder = Host.CreateApplicationBuilder(args);

    builder.Services.AddWindowsService(o => o.ServiceName = "TempMonitorAgent");
    builder.Services.AddSerilog();

    // Core state + telemetry
    builder.Services.AddSingleton<AgentState>();
    builder.Services.AddSingleton<ISensorSource, SensorReader>();
    builder.Services.AddSingleton(sp =>
        SystemInfo.Read(sp.GetRequiredService<ILoggerFactory>().CreateLogger("SystemInfo")));
    builder.Services.AddSingleton<TelemetryReporter>();

    // Fleet command channel
    builder.Services.AddSingleton<FleetClient>();
    // DeployPackageExecutor takes the downloader as an interface (so its verify/run/detect
    // logic is testable without a hub); FleetClient is the real implementation, and must
    // resolve to the SAME singleton that holds this agent's enrollment token.
    builder.Services.AddSingleton<IPackageDownloader>(sp => sp.GetRequiredService<FleetClient>());
    builder.Services.AddSingleton<CommandDispatcher>();
    // Persistent interactive shells live here (singleton, disposed at host shutdown).
    builder.Services.AddSingleton<ShellSessionManager>();
    // ConPTY terminals (the interactive Terminal tab). Same lifetime reasoning as above --
    // a session outlives the command that opened it only in the sense that the command IS
    // the session, so the registry must be a singleton the host disposes.
    builder.Services.AddSingleton<PtySessionManager>();
    // ShellOpenExecutor takes the pty endpoints as an interface (testable without a hub);
    // it must resolve to the SAME FleetClient singleton that holds the enrollment token.
    builder.Services.AddSingleton<IPtyChannel>(sp => sp.GetRequiredService<FleetClient>());
    builder.Services.AddSingleton<ICommandExecutor, RestartExecutor>();
    builder.Services.AddSingleton<ICommandExecutor, ShutdownExecutor>();
    builder.Services.AddSingleton<ICommandExecutor, RenameExecutor>();
    builder.Services.AddSingleton<ICommandExecutor, GpUpdateExecutor>();
    builder.Services.AddSingleton<ICommandExecutor, InstallAppExecutor>();
    builder.Services.AddSingleton<ICommandExecutor, RunScriptExecutor>();
    builder.Services.AddSingleton<ICommandExecutor, DeployPackageExecutor>();
    // Per-PC file backups (roadmap #1b). Takes FleetClient directly rather than through an
    // interface: unlike the package downloader there is nothing to fake usefully — the
    // testable parts (path expansion, the envelope) are separate classes with their own
    // tests, and what remains here is I/O against a real filesystem.
    builder.Services.AddSingleton<ICommandExecutor, BackupFilesExecutor>();
    builder.Services.AddSingleton<ICommandExecutor, RestoreFilesExecutor>();
    builder.Services.AddSingleton<ICommandExecutor, ShellInputExecutor>();
    builder.Services.AddSingleton<ICommandExecutor, ShellSignalExecutor>();
    builder.Services.AddSingleton<ICommandExecutor, ShellResetExecutor>();
    builder.Services.AddSingleton<ICommandExecutor, ShellOpenExecutor>();
    // Remote view/control (roadmap #2): session-injects the capture/control helper.
    builder.Services.AddSingleton<ICommandExecutor, StartRemoteSessionExecutor>();
    // Virtual display for headless machines: without a display output there is nothing for
    // Desktop Duplication to duplicate, so a monitorless box streams a black screen.
    builder.Services.AddSingleton<ICommandExecutor, InstallVirtualDisplayExecutor>();
    builder.Services.AddSingleton<ICommandExecutor, UninstallVirtualDisplayExecutor>();
    builder.Services.AddSingleton<ICommandExecutor, SetVirtualDisplayModeExecutor>();
    builder.Services.AddSingleton<ICommandExecutor, RefreshRemoteInventoryExecutor>();
    builder.Services.AddSingleton<ICommandExecutor>(_ => new StubExecutor("install_driver"));
    builder.Services.AddSingleton<ICommandExecutor>(_ => new StubExecutor("update_bios"));

    // Self-update
    builder.Services.AddSingleton<SelfUpdater>();

    builder.Services.AddHostedService<Worker>();
    // Remote view/control (roadmap #2): brings the capture helper back when its Windows session
    // is destroyed (i.e. the moment an operator signs in at the logon screen), and hosts the
    // secure-attention pipe -- SendSAS is only honoured for a real service, which this is and
    // the session-injected helper is not.
    builder.Services.AddHostedService<RemoteSessionSupervisor>();

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

// The helper branch above returns its own exit code; the service path ends here. (host.Run
// blocks until the service stops, so reaching this is the normal, clean shutdown.)
return 0;
