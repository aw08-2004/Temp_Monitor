using Serilog;
using TempMonitorAgent;
using TempMonitorAgent.Backup;
using TempMonitorAgent.Bios;
using TempMonitorAgent.Fleet;
using TempMonitorAgent.Fleet.Executors;
using TempMonitorAgent.Fleet.Shell;
using TempMonitorAgent.Files;
using TempMonitorAgent.Network;
using TempMonitorAgent.Remote;
using TempMonitorAgent.State;
using TempMonitorAgent.Telemetry;
using TempMonitorAgent.Update;
using TempMonitorAgent.UserMessage;

// Remote view/control (roadmap #2): the service session-injects THIS SAME BINARY with
// --remote-helper <session-file> into the interactive desktop. That process is not a service
// -- it must not build the Windows Service host or start the telemetry loop -- so branch to
// the helper before anything else touches the service's logger or host.
if (RemoteHelper.TryGetSessionFileArg(args) is { } remoteSessionFile)
    return RemoteHelper.Run(remoteSessionFile);

// A rule's message to the person at the PC. Same shape and same reason as the remote helper
// above: session 0 has no desktop, so a dialog created there is created onto nothing. The
// service session-injects this binary with --show-message <request-file>; this process shows
// the dialog and writes the answer beside the request.
if (MessageHelper.TryGetRequestFileArg(args) is { } messageRequestFile)
    return MessageHelper.Run(messageRequestFile);

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
// diagnosable. Console sink too,
// useful when run interactively for testing.
// Created AND re-ACLed before the first write: the inherited ACL from C:\ProgramData lets
// any local user create files in here and in every subdirectory below it, which is a
// privilege-escalation primitive against the self-updater's staging dir. See StateDirectory.
Directory.CreateDirectory(AgentConfig.ProgramDataDir);
var aclNote = StateDirectory.Harden(AgentConfig.ProgramDataDir);
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
if (aclNote is { Length: > 0 }) Log.Information("{Note}", aclNote);

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
    // Firmware (roadmap #9), in three parts: re-read the settings inventory on demand, write
    // settings and verify them by re-reading, and flash the BIOS itself. update_bios is no
    // longer stubbed -- see UpdateBiosExecutor, which stages the manufacturer's own image and
    // deliberately does not reboot or claim success, because the flash happens during POST.
    builder.Services.AddSingleton<ICommandExecutor, RefreshBiosInventoryExecutor>();
    builder.Services.AddSingleton<ICommandExecutor, SetBiosSettingsExecutor>();
    builder.Services.AddSingleton<ICommandExecutor, UpdateBiosExecutor>();
    // Wake-on-LAN (roadmap #10). wake_machine is the odd one in this whole list: it is about
    // ANOTHER machine -- the hub hands it to an awake peer on a sleeping PC's subnet, because
    // a hub-sent broadcast reaches only the hub's own segment. prepare_wake is its
    // precondition half, turning this machine's own wake flags on and Fast Startup off.
    builder.Services.AddSingleton<ICommandExecutor, WakeMachineExecutor>();
    builder.Services.AddSingleton<ICommandExecutor, PrepareWakeExecutor>();
    // The machine Processes card. There is no list_processes executor to go with these:
    // reading the list is not a command at all, it rides the heartbeat while an operator has
    // the card open (see Telemetry/ProcessReporter). These two are the half that CHANGES the
    // machine, and both refuse a pid whose process no longer answers to the name the operator
    // clicked -- see ProcessGuard.
    builder.Services.AddSingleton<ICommandExecutor, KillProcessExecutor>();
    builder.Services.AddSingleton<ICommandExecutor, RestartProcessExecutor>();
    // Messages to the person at the PC (rules engine). The only command whose subject is a
    // human: it session-injects a dialog and reports back which button was pressed, which the
    // hub maps onto follow-up actions. The agent deliberately never learns what a button
    // means -- see ShowMessageExecutor.
    builder.Services.AddSingleton<ICommandExecutor, ShowMessageExecutor>();
    // Probe collection for the rules engine: read one specific registry value, file version
    // or WMI property that no regular telemetry carries. Four of its five kinds cannot change
    // the machine; the fifth (script) is gated behind a hub setting that starts off.
    builder.Services.AddSingleton<ICommandExecutor, CollectProbeExecutor>();
    // The remote file explorer. Note that listing IS a command here, unlike the process
    // list above: browsing is human-paced (one click, one listing, no poll behind it), and
    // "who opened this folder, and when" is a question the hub's audit trail should be able
    // to answer -- see hub/files.py's FILE_COMMANDS. The listing itself is POSTed to the hub
    // rather than returned as command output, because a folder of two thousand entries does
    // not belong in a terminal transcript.
    builder.Services.AddSingleton<ICommandExecutor, ListDirectoryExecutor>();
    builder.Services.AddSingleton<ICommandExecutor, FileOperationExecutor>();
    builder.Services.AddSingleton<ICommandExecutor, FetchFileExecutor>();
    builder.Services.AddSingleton<ICommandExecutor, PushFileExecutor>();
    // Opening is the one file command that does not touch the disk: it starts something,
    // either as the signed-in user on their own desktop or as SYSTEM with no desktop at all.
    // The operator names which; see OpenItemExecutor for why that is not a default.
    builder.Services.AddSingleton<ICommandExecutor, OpenItemExecutor>();
    // Patch installs (roadmap #14). Note what this executor does NOT do: it never reports an
    // update as installed. It stages what it can and asks for a restart; the hub decides the
    // outcome later by observing that the machine has stopped offering the update. See
    // InstallPatchesExecutor and hub/patches.py confirm_from_inventory.
    builder.Services.AddSingleton<ICommandExecutor, TempMonitorAgent.Patch.InstallPatchesExecutor>();
    builder.Services.AddSingleton<ICommandExecutor>(_ => new StubExecutor("install_driver"));

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
