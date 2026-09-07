using System.Collections.Concurrent;
using Microsoft.Extensions.Logging;
using FleetHubAgent.Fleet;
using FleetHubAgent.Telemetry;

namespace FleetHubAgent;

/// <summary>
/// The agent's work, run as INDEPENDENT CONCURRENT LOOPS rather than one serial tick:
///
///   * telemetry -- read the sensors and report them
///   * heartbeat -- liveness; the call that decides whether the machine reads online
///   * commands  -- poll, claim, and dispatch fleet commands
///
/// **Why they are separate**, which is a lesson the Windows agent learned the hard way and
/// every port inherits rather than repeats: in a serial loop the slowest step sets the latency
/// of every other one. A sensor read stalling, or a heartbeat sitting out its full HTTP
/// timeout because the hub was slow, delays COMMAND POLLING by exactly that long -- so an
/// operator watches a command sit in "queued" for as long as the unrelated work takes. Worse,
/// the machine can read offline (the hub's 90-second window) because its own telemetry post is
/// queued in front of its heartbeat.
///
/// **This is a plain class, not a BackgroundService, and that is the one structural difference
/// from agent-linux's Worker.** There is no generic host on Android. The lifecycle owner is a
/// foreground Service, which the system starts, stops, and may kill outright; it calls
/// <see cref="RunAsync"/> and cancels the token when it is torn down. Keeping the loops free
/// of Microsoft.Extensions.Hosting is also what keeps this project referenceable from a plain
/// xunit run on a workstation -- see FleetHubAgent.Core.csproj.
/// </summary>
public sealed class AgentLoops(
    ILogger<AgentLoops> log,
    ISensorSource sensors,
    IUptimeSource uptime,
    TelemetryReporter reporter,
    FleetClient fleet,
    CommandDispatcher dispatcher,
    MachineNameProvider names,
    IEnrollmentSecretSource secrets)
{
    /// <summary>In-flight commands, keyed by id. Bounds concurrency and keeps the poll loop
    /// from re-dispatching something already running.</summary>
    private readonly ConcurrentDictionary<string, Task> _running = new();

    /// <summary>Serialises enrollment. Both the heartbeat and command loops need an enrolled
    /// agent and either may be first, but enrolling twice would mint a second identity for one
    /// device and duplicate it in the fleet.</summary>
    private readonly SemaphoreSlim _enrollGate = new(1, 1);

    private const int EnrollRetrySeconds = 30;
    private DateTime _lastEnrollAttemptUtc = DateTime.MinValue;
    private bool _telemetryOnlyLogged;
    private bool _noTempLogged;

    /// <summary>When the command loop last saw a command. Keeps it on the fast cadence for
    /// CommandBurstSeconds afterwards -- see AgentConfig.</summary>
    private DateTime _lastCommandUtc = DateTime.MinValue;

    public async Task RunAsync(CancellationToken stoppingToken)
    {
        log.LogInformation("FleetHub Android agent v{Version} - machine: {Machine} - hub: {Hub}",
            AgentConfig.Version, names.Current, AgentConfig.HubBase);

        // Task.Run, not a bare call: each loop must get its own thread-pool context so a
        // synchronous stretch inside one (a thermal-zone walk across two dozen zones, a StatFs
        // against a slow SD card) runs on that loop's thread and nowhere near the others.
        var loops = new[]
        {
            Task.Run(() => TelemetryLoopAsync(stoppingToken), CancellationToken.None),
            Task.Run(() => HeartbeatLoopAsync(stoppingToken), CancellationToken.None),
            Task.Run(() => CommandLoopAsync(stoppingToken), CancellationToken.None),
        };

        // One loop failing outright must not silently leave the agent half-running, so wait on
        // all of them and log whatever ended first.
        await Task.WhenAll(loops);
        log.LogInformation("All loops stopped");
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

                var snapshot = sensors.Read();
                if (snapshot.CpuTemp is double temp)
                {
                    await reporter.ReportAsync(
                        temp,
                        includeSensors ? snapshot.Sensors : null,
                        includeUptime ? uptime.UptimeSeconds() : null,
                        ct);

                    if (includeSensors) lastSensor = now;
                    if (includeUptime) lastUptime = now;
                }
                else if (!_noTempLogged)
                {
                    // **No temperature means no report at all**, matching every other agent --
                    // /api/report requires a numeric temp and there is no honest number to
                    // send. The consequence is worth stating in full: this device will still
                    // appear in the console (the heartbeat below keeps it online once
                    // enrolled) but its model, OS, storage and memory all ride on /api/report,
                    // so they will all stay blank.
                    //
                    // This should be unreachable on Android and that is a deliberate property
                    // of AndroidSensorReader, not luck: battery temperature is available on
                    // every device that has a battery, which is why it is the last fallback
                    // there. Reaching here means something stranger than an unusual
                    // motherboard -- an emulator with no battery, most likely. Logged ONCE.
                    _noTempLogged = true;
                    log.LogWarning(
                        "No temperature reading available; skipping /api/report. This device " +
                        "will show as online but without inventory (model, OS, storage, memory).");
                }
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { break; }
            catch (Exception e) { log.LogWarning(e, "Telemetry tick failed"); }

            if (!await DelayAsync(AgentConfig.IntervalSeconds, ct)) break;
        }
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
                if (await EnsureEnrolledAsync(ct))
                    await fleet.HeartbeatAsync(ct);
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { break; }
            catch (Exception e) { log.LogWarning(e, "Heartbeat tick failed"); }

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
            catch (Exception e) { log.LogWarning(e, "Command poll failed"); }

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
        var poll = await fleet.PollCommandsAsync(AgentConfig.CommandWaitSeconds, ct);
        if (poll.Commands.Count > 0) _lastCommandUtc = DateTime.UtcNow;

        foreach (var cmd in poll.Commands)
        {
            if (string.IsNullOrEmpty(cmd.Id)) continue;

            // Already running. The hub can hand back a command we claimed but have not
            // answered yet, and starting it a second time would run it twice.
            if (_running.ContainsKey(cmd.Id)) continue;

            if (_running.Count >= AgentConfig.MaxConcurrentCommands)
            {
                // Left in the hub's queue rather than failed: it will be offered again on the
                // next poll, when a slot has freed. Failing it here would tell the operator
                // their command was rejected when it was only postponed by seconds.
                log.LogInformation("At the {Max}-command concurrency cap; leaving {Id} queued",
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
            var result = await dispatcher.ExecuteAsync(cmd, onOutput: null, ct);
            // CancellationToken.None: a result must reach the hub even while the service is
            // stopping. The alternative is a command the console shows as running forever.
            await fleet.ReportResultAsync(cmd.Id, result, CancellationToken.None);
        }
        catch (Exception e)
        {
            log.LogWarning(e, "Command {Id} ({Type}) failed outright", cmd.Id, cmd.Type);
            try
            {
                await fleet.ReportResultAsync(
                    cmd.Id, CommandResult.Fail($"agent error: {e.Message}"), CancellationToken.None);
            }
            catch { /* the hub is unreachable; the command times out on its side */ }
        }
        finally
        {
            _running.TryRemove(cmd.Id, out _);
        }
    }

    // ------------------------------------------------------------------ enrollment

    /// <summary>Enroll if needed, at most once every EnrollRetrySeconds, and never twice
    /// concurrently. Returns whether the agent has an identity.</summary>
    private async Task<bool> EnsureEnrolledAsync(CancellationToken ct)
    {
        if (fleet.IsEnrolled) return true;

        await _enrollGate.WaitAsync(ct);
        try
        {
            if (fleet.IsEnrolled) return true;
            if ((DateTime.UtcNow - _lastEnrollAttemptUtc).TotalSeconds < EnrollRetrySeconds)
                return false;
            _lastEnrollAttemptUtc = DateTime.UtcNow;

            // Re-read every attempt rather than caching at construction. Unlike a systemd
            // EnvironmentFile, the secret here can APPEAR while the agent is running -- an MDM
            // pushes a managed configuration, or someone types it on the setup screen -- and a
            // value cached at startup would leave the device telemetry-only until it was
            // restarted, which nobody would think to do.
            var secret = secrets.Read();
            if (await fleet.EnsureEnrolledAsync(secret, ct)) return true;

            if (!_telemetryOnlyLogged)
            {
                // Once. A device with no secret is a permanent state until someone supplies
                // one, and saying so every thirty seconds forever would bury the log.
                _telemetryOnlyLogged = true;
                log.LogWarning(
                    "Running telemetry-only: this device reports but cannot receive commands. " +
                    "Supply the hub's enrollment secret through managed configuration or the " +
                    "setup screen.");
            }
            return false;
        }
        finally
        {
            _enrollGate.Release();
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

/// <summary>
/// Where the shared enrollment secret comes from, asked afresh on every enrollment attempt.
///
/// **An interface because the answer changes at runtime on Android and nowhere else.** The
/// Windows and Linux agents read an environment variable or a 0600 file once at startup, both
/// of which are fixed for the life of the process. Here the secret may arrive from an MDM's
/// managed configuration minutes after the device booted, or be typed on the setup screen by
/// whoever is holding it -- so this is a question, not a value.
/// </summary>
public interface IEnrollmentSecretSource
{
    /// <summary>The current secret, or null when none has been supplied yet. Never throws --
    /// it is called from the enrollment path of two unattended loops.</summary>
    string? Read();
}
