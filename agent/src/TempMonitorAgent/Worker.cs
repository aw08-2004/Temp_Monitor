using System.Collections.Concurrent;
using Microsoft.Extensions.Logging;
using Microsoft.Win32;
using TempMonitorAgent.Fleet;
using TempMonitorAgent.Fleet.Shell;
using TempMonitorAgent.State;
using TempMonitorAgent.Telemetry;
using TempMonitorAgent.Update;

namespace TempMonitorAgent;

/// <summary>
/// The service's main loop. Ticks every INTERVAL seconds: reports telemetry (with the
/// full sensor block and uptime on their own slower cadences), and on the command
/// cadence enrolls/heartbeats/polls the fleet channel and dispatches any claimed commands.
/// Periodically (and when the hub hints a newer version) it checks for a signed
/// self-update; applying one exits the process so the SCM restarts onto the new binary.
///
/// Claimed commands run on their own tasks rather than inline, so a long one (run_script
/// can take its full 600s) never stalls telemetry or heartbeats. Keeping the loop ticking
/// is what stops a machine reading "offline" while it is busy running your command.
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
    private readonly ShellSessionManager _shells;

    /// <summary>In-flight commands, keyed by id. Bounds concurrency and keeps the poll
    /// loop from re-dispatching something already running.</summary>
    private readonly ConcurrentDictionary<string, Task> _running = new();

    private readonly string? _enrollmentSecret;

    public Worker(
        ILogger<Worker> log, AgentState state, ISensorSource sensors,
        TelemetryReporter reporter, FleetClient fleet, CommandDispatcher dispatcher,
        SelfUpdater updater, ShellSessionManager shells)
    {
        _log = log;
        _state = state;
        _sensors = sensors;
        _reporter = reporter;
        _fleet = fleet;
        _dispatcher = dispatcher;
        _updater = updater;
        _shells = shells;
        _enrollmentSecret = ReadEnrollmentSecret();
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        _state.EnsureStateDir();
        // Restore the hub-delivered config before the first sensor read. Without this a
        // reboot or self-update would run on compiled defaults until the first heartbeat
        // lands, so the box would read the "wrong" sensor for ~10 seconds every time.
        RuntimeConfigStore.Set(_state.LoadRuntimeConfig());
        _log.LogInformation("TempMonitor agent v{Version} - machine: {Machine} - hub: {Hub}",
            AgentConfig.Version, AgentConfig.MachineName, AgentConfig.HubBase);

        _updater.ReconcileAfterBoot();
        if (await _updater.CheckAndApplyAsync(stoppingToken)) { Restart(); return; }

        var now = DateTime.UtcNow;
        var lastTemp = DateTime.MinValue;
        var lastSensor = DateTime.MinValue;
        var lastUptime = DateTime.MinValue;
        var lastCommandPoll = DateTime.MinValue;
        var lastUpdateCheck = now;
        bool updateDue = false;

        while (!stoppingToken.IsCancellationRequested)
        {
            now = DateTime.UtcNow;
            // While an operator's shell is mid-submission we tick fast, so typed shell_input
            // reaches the shell within ~1s instead of waiting out the normal poll cadence.
            bool shellActive = _shells.AnyActiveSubmission;
            try
            {
                // --- Telemetry (own cadence, so fast-ticking doesn't spam temp reports) ---
                if ((now - lastTemp).TotalSeconds >= AgentConfig.IntervalSeconds)
                {
                    bool includeSensors = (now - lastSensor).TotalSeconds >= AgentConfig.SensorIntervalSeconds;
                    bool includeUptime = (now - lastUptime).TotalSeconds >= AgentConfig.UptimeIntervalSeconds;

                    var snapshot = _sensors.Read();
                    if (snapshot.CpuTemp is double temp)
                    {
                        var result = await _reporter.ReportAsync(
                            temp,
                            includeSensors ? snapshot.Sensors : null,
                            includeUptime ? SystemInfo.UptimeSeconds() : null,
                            stoppingToken);

                        if (includeSensors) lastSensor = now;
                        if (includeUptime) lastUptime = now;

                        if (result.LatestVersion is { Length: > 0 } lv &&
                            VersionUtil.Compare(lv, AgentConfig.Version) > 0)
                        {
                            updateDue = true;
                        }
                    }
                    else
                    {
                        _log.LogWarning("No CPU temperature reading this cycle");
                    }
                    lastTemp = now;
                }

                // --- Fleet command channel -------------------------------------
                double pollEvery = shellActive
                    ? AgentConfig.CommandPollFastSeconds
                    : AgentConfig.CommandPollSeconds;
                if ((now - lastCommandPoll).TotalSeconds >= pollEvery)
                {
                    await RunFleetCycleAsync(stoppingToken);
                    lastCommandPoll = now;
                }

                // --- Self-update -----------------------------------------------
                // Never swap the binary out from under a running command: applying an
                // update exits the process (code 17) for the SCM to restart, which would
                // kill a half-finished script and report nothing back. This couldn't
                // happen while commands were awaited inline; now that they run on their
                // own tasks, it can. Deferring costs at most one command's runtime on a
                // weekly check, and `updateDue` is left set so the next tick retries.
                bool updateWanted =
                    updateDue || (now - lastUpdateCheck).TotalSeconds >= AgentConfig.UpdateIntervalSeconds;
                if (updateWanted && !_running.IsEmpty)
                {
                    _log.LogInformation("Update deferred: {N} command(s) still running", _running.Count);
                    updateDue = true;
                }
                else if (updateWanted)
                {
                    updateDue = false;
                    lastUpdateCheck = now;
                    if (await _updater.CheckAndApplyAsync(stoppingToken)) { Restart(); return; }
                }
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                break;
            }
            catch (Exception e)
            {
                _log.LogWarning(e, "Loop iteration failed");
            }

            var loopDelay = _shells.AnyActiveSubmission
                ? AgentConfig.CommandPollFastSeconds
                : AgentConfig.IntervalSeconds;
            try { await Task.Delay(TimeSpan.FromSeconds(loopDelay), stoppingToken); }
            catch (OperationCanceledException) { break; }
        }
    }

    private async Task RunFleetCycleAsync(CancellationToken ct)
    {
        if (!_fleet.IsEnrolled)
            await _fleet.EnsureEnrolledAsync(_enrollmentSecret, ct);

        if (!_fleet.IsEnrolled) return;

        await _fleet.HeartbeatAsync(ct);

        var commands = await _fleet.PollCommandsAsync(ct);
        foreach (var cmd in commands)
        {
            // Commands used to be awaited inline here, which meant a run_script blocked
            // this method -- and therefore the whole main loop -- for up to its 600s
            // timeout. Telemetry and heartbeats stopped for the duration, so a machine
            // went "offline" (90s window) while its own command was still running. Now
            // each command runs on its own task and the loop keeps ticking, which is also
            // what makes streamed output worth watching.
            // Session-control commands (shell_input/signal/reset) steer a shell that is
            // ALREADY running a submission -- refusing them for concurrency would deadlock the
            // very command holding a slot. They're near-instant, so let them straight through.
            bool isControl = cmd.Type is "shell_input" or "shell_signal" or "shell_reset";
            if (!isControl && _running.Count >= AgentConfig.MaxConcurrentCommands)
            {
                _log.LogWarning("At {Max} concurrent commands; refusing {Type} {Id}",
                    AgentConfig.MaxConcurrentCommands, cmd.Type, cmd.Id);
                await _fleet.ReportResultAsync(
                    cmd.Id, CommandResult.Fail("agent busy: too many commands already running"), ct);
                continue;
            }
            _running[cmd.Id] = Task.Run(() => RunOneAsync(cmd, ct), ct);
        }

        // Reap finished entries so the dictionary can't grow without bound.
        foreach (var (id, task) in _running.ToArray())
            if (task.IsCompleted) _running.TryRemove(id, out _);
    }

    private async Task RunOneAsync(FleetCommand cmd, CancellationToken ct)
    {
        await using var streamer = new OutputStreamer(_fleet, cmd.Id, _log);
        try
        {
            var result = await _dispatcher.ExecuteAsync(cmd, streamer.Add, ct);
            // ORDER MATTERS: the console stops polling for output once the command hits a
            // terminal status, so the last chunks must be flushed BEFORE the result lands
            // or the operator silently loses the tail of what they ran.
            await streamer.CompleteAsync(ct);
            await _fleet.ReportResultAsync(cmd.Id, result, ct);
        }
        catch (Exception e)
        {
            _log.LogWarning(e, "Command {Id} failed outside the executor", cmd.Id);
            try { await _fleet.ReportResultAsync(cmd.Id, CommandResult.Fail($"agent error: {e.Message}"), ct); }
            catch { /* the hub will expire it */ }
        }
        finally
        {
            _running.TryRemove(cmd.Id, out _);
        }
    }

    private void Restart()
    {
        _log.LogInformation("Exiting {Code} to restart onto the updated binary", AgentConfig.RestartExitCode);
        Environment.Exit(AgentConfig.RestartExitCode);
    }

    /// <summary>Enrollment secret: env override (testing) else the installer-written
    /// HKLM value. Absent => the agent runs telemetry-only until enrolled.</summary>
    private static string? ReadEnrollmentSecret()
    {
        var env = Environment.GetEnvironmentVariable("AGENT_ENROLLMENT_SECRET");
        if (!string.IsNullOrEmpty(env)) return env;

        // New key first, then the pre-rename one: an agent that self-updates onto a
        // FleetHub build still has its secret under the legacy key until the installer
        // is re-run, and losing it would drop the box back to telemetry-only.
        foreach (var path in new[] { AgentConfig.RegistryKeyPath, AgentConfig.LegacyRegistryKeyPath })
        {
            try
            {
                using var key = Registry.LocalMachine.OpenSubKey(path);
                if (key?.GetValue(AgentConfig.RegistryEnrollmentSecretValue) is string s
                    && !string.IsNullOrEmpty(s))
                    return s;
            }
            catch { /* try the next key */ }
        }
        return null;
    }
}
