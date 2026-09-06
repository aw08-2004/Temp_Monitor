using System.Collections.Concurrent;
using System.Globalization;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using FleetHubAgent.Fleet;
using FleetHubAgent.State;
using FleetHubAgent.Telemetry;
using FleetHubAgent.Update;

namespace FleetHubAgent;

/// <summary>
/// The service's background work, run as INDEPENDENT CONCURRENT LOOPS rather than one serial
/// tick:
///
///   * telemetry -- read the sensors and report them
///   * heartbeat -- liveness; the call that decides whether the machine reads online
///   * commands  -- poll, claim, and dispatch fleet commands
///
/// **Why they are separate**, which is a lesson the Windows agent learned the hard way and
/// this one inherits rather than repeats: in a serial loop the slowest step sets the latency
/// of every other one. A sensor read stalling behind a busy disk, or a heartbeat sitting out
/// its full HTTP timeout because the hub was slow, delays COMMAND POLLING by exactly that long
/// -- so an operator watches a command sit in "queued" for as long as the unrelated work
/// takes. Worse, the machine can read offline (the hub's 90-second window) because its own
/// telemetry post is queued in front of its heartbeat.
///
/// The Windows agent runs six loops; this one runs three. The missing three (inventory,
/// processes, self-update) back features this agent does not have -- see AgentConfig.Version
/// for why it deliberately sits below the console's version gates for them, and README.md for
/// what is next.
/// </summary>
public sealed class Worker : BackgroundService
{
    private readonly ILogger<Worker> _log;
    private readonly AgentState _state;
    private readonly ISensorSource _sensors;
    private readonly TelemetryReporter _reporter;
    private readonly FleetClient _fleet;
    private readonly CommandDispatcher _dispatcher;
    private readonly SelfUpdater _updater;

    /// <summary>In-flight commands, keyed by id. Bounds concurrency and keeps the poll loop
    /// from re-dispatching something already running.</summary>
    private readonly ConcurrentDictionary<string, Task> _running = new();

    /// <summary>Serialises enrollment. Both the heartbeat and command loops need an enrolled
    /// agent and either may be first, but enrolling twice would mint a second identity for one
    /// machine and duplicate it in the fleet.</summary>
    private readonly SemaphoreSlim _enrollGate = new(1, 1);

    private const int EnrollRetrySeconds = 30;
    private DateTime _lastEnrollAttemptUtc = DateTime.MinValue;
    private bool _telemetryOnlyLogged;
    private bool _noTempLogged;

    /// <summary>When the command loop last saw a command. Keeps it on the fast cadence for
    /// CommandBurstSeconds afterwards -- see AgentConfig.</summary>
    private DateTime _lastCommandUtc = DateTime.MinValue;

    private readonly string? _enrollmentSecret;

    public Worker(
        ILogger<Worker> log, AgentState state, ISensorSource sensors,
        TelemetryReporter reporter, FleetClient fleet, CommandDispatcher dispatcher,
        SelfUpdater updater)
    {
        _log = log;
        _state = state;
        _sensors = sensors;
        _reporter = reporter;
        _fleet = fleet;
        _dispatcher = dispatcher;
        _updater = updater;
        _enrollmentSecret = ReadEnrollmentSecret(log);
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        if (_state.EnsureStateDir() is { Length: > 0 } note) _log.LogWarning("{Note}", note);

        _log.LogInformation("FleetHub Linux agent v{Version} - machine: {Machine} - hub: {Hub}",
            AgentConfig.Version, AgentConfig.MachineName, AgentConfig.HubBase);

        // The boot-time update check stays in FRONT of the loops. Applying an update exits the
        // process, and there is no point starting three loops only to tear them down again --
        // and more importantly, a machine that has been off for a month should come back on the
        // current build before it starts reporting, not a week later when the weekly timer
        // first fires.
        _updater.ReconcileAfterBoot();
        if (await _updater.CheckAndApplyAsync(stoppingToken)) { Restart(); return; }

        // Task.Run, not a bare call: each loop must get its own thread-pool context so a
        // synchronous stretch inside one (a hwmon walk across a dozen chips, a DriveInfo stat
        // against a hung NFS mount) runs on that loop's thread and nowhere near the others.
        var loops = new[]
        {
            Task.Run(() => TelemetryLoopAsync(stoppingToken), CancellationToken.None),
            Task.Run(() => HeartbeatLoopAsync(stoppingToken), CancellationToken.None),
            Task.Run(() => CommandLoopAsync(stoppingToken), CancellationToken.None),
            Task.Run(() => UpdateLoopAsync(stoppingToken), CancellationToken.None),
        };

        // One loop failing outright must not silently leave the agent half-running, so wait on
        // all of them and log whatever ended first.
        await Task.WhenAll(loops);
        _log.LogInformation("All loops stopped");
    }

    // ------------------------------------------------------------------ telemetry

    private async Task TelemetryLoopAsync(CancellationToken ct)
    {
        var lastSensor = DateTime.MinValue;
        var lastUptime = DateTime.MinValue;

        while (!ct.IsCancellationRequested)
        {
            try
            {
                var now = DateTime.UtcNow;
                var includeSensors = (now - lastSensor).TotalSeconds >= AgentConfig.SensorIntervalSeconds;
                var includeUptime = (now - lastUptime).TotalSeconds >= AgentConfig.UptimeIntervalSeconds;

                var snapshot = _sensors.Read();
                if (snapshot.CpuTemp is double temp)
                {
                    var result = await _reporter.ReportAsync(
                        temp,
                        includeSensors ? snapshot.Sensors : null,
                        includeUptime ? UptimeSeconds() : null,
                        ct);

                    if (includeSensors) lastSensor = now;
                    if (includeUptime) lastUptime = now;

                    // Proof that an updated build can do its job, not merely start -- see
                    // SelfUpdater.ConfirmRunningBuild. Free after the first success.
                    if (result.Sent) _updater.ConfirmRunningBuild();
                }
                else if (!_noTempLogged)
                {
                    // **No temperature means no report at all**, matching the Windows agent --
                    // /api/report requires a numeric temp and there is no honest number to
                    // send. The consequence is worth stating in full because it is the one
                    // failure mode a Linux box has that a Windows box does not: this machine
                    // will still appear in the console (the heartbeat below keeps it online
                    // once enrolled) but its model, serial, OS, disks and memory all ride on
                    // /api/report, so they will all stay blank.
                    //
                    // Every thermal source is tried before we get here, including the ACPI
                    // zones a VM usually has -- see ProcSensorReader. Logged ONCE: this is a
                    // permanent property of the hardware, not an incident.
                    _noTempLogged = true;
                    _log.LogWarning(
                        "No temperature reading available; skipping /api/report. This machine " +
                        "will show as online but without inventory (model, OS, disks, memory).");
                }
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { break; }
            catch (Exception e) { _log.LogWarning(e, "Telemetry tick failed"); }

            if (!await DelayAsync(AgentConfig.IntervalSeconds, ct)) break;
        }
    }

    /// <summary>Seconds since boot, from /proc/uptime's first field.
    ///
    /// Deliberately not Environment.TickCount64, which on Linux is CLOCK_MONOTONIC and
    /// therefore counts from process start under some runtimes and excludes suspend time --
    /// "how long has this machine been up" is a question about the machine, not about us.</summary>
    internal static long? UptimeSeconds()
    {
        try
        {
            var first = File.ReadAllText("/proc/uptime").Split(' ')[0];
            return double.TryParse(first, NumberStyles.Float, CultureInfo.InvariantCulture, out var s)
                ? (long)s : null;
        }
        catch { return null; }
    }

    // ------------------------------------------------------------------ heartbeat

    /// <summary>Liveness. Kept apart from command polling because this is the call that
    /// decides whether the machine reads online, and it must not queue behind anything.</summary>
    private async Task HeartbeatLoopAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try
            {
                // The heartbeat confirms an update too, and must: a machine with no thermal
                // sensor never reports at all (see the telemetry loop), so gating confirmation
                // on /api/report alone would leave every VM holding its previous binary
                // forever while being perfectly healthy.
                if (await EnsureEnrolledAsync(ct) && await _fleet.HeartbeatAsync(ct))
                    _updater.ConfirmRunningBuild();
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { break; }
            catch (Exception e) { _log.LogWarning(e, "Heartbeat tick failed"); }

            if (!await DelayAsync(AgentConfig.HeartbeatSeconds, ct)) break;
        }
    }

    // ------------------------------------------------------------------ commands

    /// <summary>Ask, claim, dispatch.
    ///
    /// The ask is a HELD request by default: the hub keeps it open while this machine's queue
    /// is empty and answers the moment something is issued, so an operator's click starts work
    /// within a round trip rather than up to CommandPollSeconds later.
    ///
    /// Which is why the sleep at the bottom is conditional. If the hub held the request, the
    /// wait already happened inside it and this comes straight back round; sleeping again
    /// would hand back the latency the hold just removed. If it declined -- push turned off,
    /// its cap full, or the request failed outright -- the ordinary cadence is still there
    /// underneath and that is what runs.</summary>
    private async Task CommandLoopAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            var held = false;
            try
            {
                if (await EnsureEnrolledAsync(ct))
                    held = await PollAndDispatchAsync(ct);
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { break; }
            catch (Exception e) { _log.LogWarning(e, "Command poll failed"); }

            if (held) continue;

            var busy = (DateTime.UtcNow - _lastCommandUtc).TotalSeconds < AgentConfig.CommandBurstSeconds;
            var every = busy ? AgentConfig.CommandPollFastSeconds : AgentConfig.CommandPollSeconds;
            if (!await DelayAsync(every, ct)) break;
        }
    }

    /// <summary>Returns whether the hub held this request open, i.e. whether the loop has
    /// already done its waiting.</summary>
    private async Task<bool> PollAndDispatchAsync(CancellationToken ct)
    {
        var poll = await _fleet.PollCommandsAsync(AgentConfig.CommandWaitSeconds, ct);
        if (poll.Commands.Count > 0) _lastCommandUtc = DateTime.UtcNow;

        foreach (var cmd in poll.Commands)
        {
            if (string.IsNullOrEmpty(cmd.Id)) continue;

            // Already running. The hub can hand back a command we claimed but have not
            // answered yet -- a run_script with a ten-minute timeout spans dozens of polls --
            // and starting it a second time would run the operator's script twice.
            if (_running.ContainsKey(cmd.Id)) continue;

            if (_running.Count >= AgentConfig.MaxConcurrentCommands)
            {
                // Left in the hub's queue rather than failed: it will be offered again on the
                // next poll, when a slot has freed. Failing it here would tell the operator
                // their command was rejected when it was only postponed by seconds.
                _log.LogInformation("At the {Max}-command concurrency cap; leaving {Id} queued",
                    AgentConfig.MaxConcurrentCommands, cmd.Id);
                break;
            }

            var task = Task.Run(() => RunCommandAsync(cmd, ct), CancellationToken.None);
            _running[cmd.Id] = task;
            // Removed by the runner itself rather than by a continuation, so the slot is
            // released after the RESULT is reported and not merely after the work finished.
        }

        return poll.Waited;
    }

    private async Task RunCommandAsync(FleetCommand cmd, CancellationToken ct)
    {
        try
        {
            var result = await _dispatcher.ExecuteAsync(cmd, onOutput: null, ct);
            // CancellationToken.None: a result must reach the hub even while the service is
            // stopping. The alternative is a command the console shows as running forever,
            // with a machine that has already rebooted underneath it.
            await _fleet.ReportResultAsync(cmd.Id, result, CancellationToken.None);
        }
        catch (Exception e)
        {
            _log.LogWarning(e, "Command {Id} ({Type}) failed outright", cmd.Id, cmd.Type);
            try
            {
                await _fleet.ReportResultAsync(
                    cmd.Id, CommandResult.Fail($"agent error: {e.Message}"), CancellationToken.None);
            }
            catch { /* the hub is unreachable; the command times out on its side */ }
        }
        finally
        {
            _running.TryRemove(cmd.Id, out _);
        }
    }

    // ------------------------------------------------------------------ updates

    /// <summary>The weekly signed-update check.
    ///
    /// Its own loop for the same reason every other one is: a 30-second manifest fetch against
    /// a slow GitHub must not sit in front of a command poll. It sleeps FIRST, because the boot
    /// path above has already checked -- starting with another check would make a service that
    /// restarts often (a machine being worked on) hammer the manifest.</summary>
    private async Task UpdateLoopAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            if (!await DelayAsync(AgentConfig.UpdateIntervalSeconds, ct)) break;
            try
            {
                if (await _updater.CheckAndApplyAsync(ct)) { Restart(); return; }
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { break; }
            catch (Exception e) { _log.LogWarning(e, "Update check failed"); }
        }
    }

    /// <summary>Leave, so systemd brings us back on the swapped binary.
    ///
    /// Environment.Exit rather than stopping the host gracefully: the binary this process is
    /// running has already been renamed aside, and every loop still running is doing work
    /// attributable to a version that is no longer installed. The unit is Restart=always with
    /// RestartSec=10, so the machine is back within ten seconds.</summary>
    private void Restart()
    {
        _log.LogInformation("Exiting {Code} to restart onto the updated binary",
            AgentConfig.RestartExitCode);
        Environment.Exit(AgentConfig.RestartExitCode);
    }

    // ------------------------------------------------------------------ enrollment

    /// <summary>Enroll if needed, at most once every EnrollRetrySeconds, and never twice
    /// concurrently. Returns whether the agent has an identity.</summary>
    private async Task<bool> EnsureEnrolledAsync(CancellationToken ct)
    {
        if (_fleet.IsEnrolled) return true;

        await _enrollGate.WaitAsync(ct);
        try
        {
            if (_fleet.IsEnrolled) return true;
            if ((DateTime.UtcNow - _lastEnrollAttemptUtc).TotalSeconds < EnrollRetrySeconds)
                return false;
            _lastEnrollAttemptUtc = DateTime.UtcNow;

            if (await _fleet.EnsureEnrolledAsync(_enrollmentSecret, ct)) return true;

            if (!_telemetryOnlyLogged)
            {
                // Once. A machine with no secret is a permanent state until someone installs
                // one, and saying so every thirty seconds forever would bury the log.
                _telemetryOnlyLogged = true;
                _log.LogWarning(
                    "Running telemetry-only: this machine reports but cannot receive commands. " +
                    "Put the enrollment secret in {File} (0600 root:root) or in ${Var}, then " +
                    "restart fleethub-agent.",
                    AgentConfig.EnrollmentSecretPath, AgentConfig.EnrollmentSecretVar);
            }
            return false;
        }
        finally
        {
            _enrollGate.Release();
        }
    }

    /// <summary>The shared enrollment secret: the environment first, then the file.
    ///
    /// The env var comes first to match the Windows agent's order, and because it is what a
    /// systemd EnvironmentFile= or a container's `-e` supplies -- an explicitly injected value
    /// should beat one left on disk by an earlier install.
    ///
    /// **The file's permissions are checked, not assumed.** This is a credential that enrolls a
    /// machine into the fleet; if it is readable by anyone but root, any local account can
    /// enroll a machine of their choosing. We refuse to use a world- or group-readable copy
    /// rather than quietly accepting it, because an agent that works despite the wrong
    /// permissions is an agent nobody ever fixes.</summary>
    private static string? ReadEnrollmentSecret(ILogger log)
    {
        var env = Environment.GetEnvironmentVariable(AgentConfig.EnrollmentSecretVar);
        if (!string.IsNullOrEmpty(env)) return env;

        var path = AgentConfig.EnrollmentSecretPath;
        try
        {
            if (!File.Exists(path)) return null;

            var mode = File.GetUnixFileMode(path);
            const UnixFileMode tooOpen =
                UnixFileMode.GroupRead | UnixFileMode.GroupWrite |
                UnixFileMode.OtherRead | UnixFileMode.OtherWrite;
            if ((mode & tooOpen) != 0)
            {
                log.LogError(
                    "Refusing to read {Path}: it is readable beyond root ({Mode}). " +
                    "Fix it with: chmod 600 {Path} && chown root:root {Path}",
                    path, mode, path, path);
                return null;
            }

            var secret = File.ReadAllText(path).Trim();
            return secret.Length > 0 ? secret : null;
        }
        catch (Exception e)
        {
            log.LogWarning("Could not read {Path}: {Msg}", path, e.Message);
            return null;
        }
    }

    /// <summary>Sleep, reporting whether we were cancelled rather than throwing. Every loop
    /// above ends by asking this, so a stop request unwinds all three the same way.</summary>
    private static async Task<bool> DelayAsync(int seconds, CancellationToken ct)
    {
        try
        {
            await Task.Delay(TimeSpan.FromSeconds(seconds), ct);
            return true;
        }
        catch (OperationCanceledException) { return false; }
    }
}
