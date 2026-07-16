using Microsoft.Extensions.Logging;
using Microsoft.Win32;
using TempMonitorAgent.Fleet;
using TempMonitorAgent.State;
using TempMonitorAgent.Telemetry;
using TempMonitorAgent.Update;

namespace TempMonitorAgent;

/// <summary>
/// The service's main loop. Ticks every INTERVAL seconds: reports telemetry (with the
/// full sensor block and uptime on their own slower cadences), and on the command
/// cadence enrolls/heartbeats/polls the fleet channel and runs any claimed commands.
/// Periodically (and when the hub hints a newer version) it checks for a signed
/// self-update; applying one exits the process so the SCM restarts onto the new binary.
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

    private readonly string? _enrollmentSecret;

    public Worker(
        ILogger<Worker> log, AgentState state, ISensorSource sensors,
        TelemetryReporter reporter, FleetClient fleet, CommandDispatcher dispatcher, SelfUpdater updater)
    {
        _log = log;
        _state = state;
        _sensors = sensors;
        _reporter = reporter;
        _fleet = fleet;
        _dispatcher = dispatcher;
        _updater = updater;
        _enrollmentSecret = ReadEnrollmentSecret();
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        _state.EnsureStateDir();
        _log.LogInformation("TempMonitor agent v{Version} - machine: {Machine} - hub: {Hub}",
            AgentConfig.Version, AgentConfig.MachineName, AgentConfig.HubBase);

        _updater.ReconcileAfterBoot();
        if (await _updater.CheckAndApplyAsync(stoppingToken)) { Restart(); return; }

        var now = DateTime.UtcNow;
        var lastSensor = DateTime.MinValue;
        var lastUptime = DateTime.MinValue;
        var lastCommandPoll = DateTime.MinValue;
        var lastUpdateCheck = now;
        bool updateDue = false;

        while (!stoppingToken.IsCancellationRequested)
        {
            now = DateTime.UtcNow;
            try
            {
                // --- Telemetry -------------------------------------------------
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

                // --- Fleet command channel -------------------------------------
                if ((now - lastCommandPoll).TotalSeconds >= AgentConfig.CommandPollSeconds)
                {
                    await RunFleetCycleAsync(stoppingToken);
                    lastCommandPoll = now;
                }

                // --- Self-update -----------------------------------------------
                if (updateDue || (now - lastUpdateCheck).TotalSeconds >= AgentConfig.UpdateIntervalSeconds)
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

            try { await Task.Delay(TimeSpan.FromSeconds(AgentConfig.IntervalSeconds), stoppingToken); }
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
            var result = await _dispatcher.ExecuteAsync(cmd, ct);
            await _fleet.ReportResultAsync(cmd.Id, result, ct);
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

        try
        {
            using var key = Registry.LocalMachine.OpenSubKey(AgentConfig.RegistryKeyPath);
            return key?.GetValue(AgentConfig.RegistryEnrollmentSecretValue) as string;
        }
        catch { return null; }
    }
}
