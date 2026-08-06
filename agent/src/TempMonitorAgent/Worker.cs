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
/// The service's background work, run as INDEPENDENT CONCURRENT LOOPS rather than one
/// serial tick:
///
///   * telemetry  -- read the sensors and report them
///   * heartbeat  -- liveness, and the channel hub config/profiles/inventory ride on
///   * commands   -- poll, claim, and dispatch fleet commands
///   * inventory  -- the slow local scans (backup profiles, logon sessions, displays)
///   * processes  -- the live process list, but only while an operator is watching one
///   * updates    -- the signed self-update check
///
/// WHY THEY ARE SEPARATE. This used to be a single loop that did all five in order, and in
/// a serial loop the slowest step sets the latency of every other one. A sensor read
/// stalling behind a busy disk, a profile scan mounting a logged-off user's hive, or a
/// heartbeat sitting out its full 10-second HTTP timeout because the hub was slow, each
/// delayed COMMAND POLLING by exactly that long -- so an operator opening a terminal or
/// starting a remote session while the machine was doing anything else watched a
/// "Connecting" pill for as long as the unrelated work took. Worse, the machine could read
/// offline (90s window) because its own telemetry post was in front of its heartbeat.
///
/// Now nothing on this list can delay anything else on it. The loops share only the
/// FleetClient (whose HttpClient is designed for concurrent use) and the enrollment gate
/// below, which serialises the one operation that must happen exactly once.
///
/// Claimed commands run on their own tasks on top of that, so a long one (run_script can
/// take its full 600s) never stalls the poll loop that claimed it.
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
    private readonly PtySessionManager _ptys;

    /// <summary>In-flight commands, keyed by id. Bounds concurrency and keeps the poll
    /// loop from re-dispatching something already running.</summary>
    private readonly ConcurrentDictionary<string, Task> _running = new();

    /// <summary>Serialises enrollment. Both the heartbeat and command loops need an
    /// enrolled agent and either may be first, but enrolling twice would mint a second
    /// identity for one machine and duplicate it in the fleet.</summary>
    private readonly SemaphoreSlim _enrollGate = new(1, 1);

    /// <summary>Minimum gap between enrollment attempts once one has failed.</summary>
    private const int EnrollRetrySeconds = 30;
    private DateTime _lastEnrollAttemptUtc = DateTime.MinValue;
    private bool _telemetryOnlyLogged;

    /// <summary>Set by the telemetry loop when the hub reports a newer version, and by the
    /// update loop's own weekly clock. Read and cleared by the update loop.</summary>
    private volatile bool _updateDue;

    /// <summary>When the command loop last saw a command. Keeps it on the fast cadence for
    /// CommandBurstSeconds afterwards -- see AgentConfig.</summary>
    private DateTime _lastCommandUtc = DateTime.MinValue;

    private readonly string? _enrollmentSecret;

    public Worker(
        ILogger<Worker> log, AgentState state, ISensorSource sensors,
        TelemetryReporter reporter, FleetClient fleet, CommandDispatcher dispatcher,
        SelfUpdater updater, ShellSessionManager shells, PtySessionManager ptys)
    {
        _log = log;
        _state = state;
        _sensors = sensors;
        _reporter = reporter;
        _fleet = fleet;
        _dispatcher = dispatcher;
        _updater = updater;
        _shells = shells;
        _ptys = ptys;
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

        // The boot-time update check stays in front of everything: applying an update exits
        // the process, and there is no point starting five loops to tear them down again.
        _updater.ReconcileAfterBoot();
        if (await _updater.CheckAndApplyAsync(stoppingToken)) { Restart(); return; }

        // Task.Run, not a bare call: each loop must get its own thread-pool context so a
        // synchronous stretch inside one (LibreHardwareMonitor's hardware walk, a registry
        // hive mount) runs on that loop's thread and nowhere near the others.
        var loops = new[]
        {
            Task.Run(() => TelemetryLoopAsync(stoppingToken), CancellationToken.None),
            Task.Run(() => HeartbeatLoopAsync(stoppingToken), CancellationToken.None),
            Task.Run(() => CommandLoopAsync(stoppingToken), CancellationToken.None),
            Task.Run(() => InventoryLoopAsync(stoppingToken), CancellationToken.None),
            Task.Run(() => ProcessLoopAsync(stoppingToken), CancellationToken.None),
            Task.Run(() => UpdateLoopAsync(stoppingToken), CancellationToken.None),
        };

        // One loop failing outright must not silently leave the agent half-running, so wait
        // on all of them and log whatever ended first.
        try { await Task.WhenAll(loops); }
        catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested) { }
        catch (Exception e) { _log.LogError(e, "A background loop ended unexpectedly"); }
    }

    // ------------------------------------------------------------------ telemetry

    /// <summary>Sensors in, /api/report out. Owns the three telemetry cadences (temp, full
    /// sensor block, uptime) and nothing else -- a slow or failing sensor read now costs
    /// only the next temperature sample.</summary>
    private async Task TelemetryLoopAsync(CancellationToken ct)
    {
        var lastSensor = DateTime.MinValue;
        var lastUptime = DateTime.MinValue;

        while (!ct.IsCancellationRequested)
        {
            try
            {
                var now = DateTime.UtcNow;
                bool includeSensors = (now - lastSensor).TotalSeconds >= AgentConfig.SensorIntervalSeconds;
                bool includeUptime = (now - lastUptime).TotalSeconds >= AgentConfig.UptimeIntervalSeconds;

                var snapshot = _sensors.Read();
                if (snapshot.CpuTemp is double temp)
                {
                    var result = await _reporter.ReportAsync(
                        temp,
                        includeSensors ? snapshot.Sensors : null,
                        includeUptime ? SystemInfo.UptimeSeconds() : null,
                        ct);

                    if (includeSensors) lastSensor = now;
                    if (includeUptime) lastUptime = now;

                    if (result.LatestVersion is { Length: > 0 } lv &&
                        VersionUtil.Compare(lv, AgentConfig.Version) > 0)
                    {
                        _updateDue = true;
                    }
                }
                else
                {
                    _log.LogWarning("No CPU temperature reading this cycle");
                }
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { break; }
            catch (Exception e) { _log.LogWarning(e, "Telemetry tick failed"); }

            if (!await DelayAsync(AgentConfig.IntervalSeconds, ct)) break;
        }
    }

    // ------------------------------------------------------------------ heartbeat

    /// <summary>Liveness, plus the config/profile/inventory payloads that ride on it. Kept
    /// apart from command polling because this is the call that decides whether the machine
    /// reads online, and it must not queue behind anything.</summary>
    private async Task HeartbeatLoopAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try
            {
                if (await EnsureEnrolledAsync(ct))
                    await _fleet.HeartbeatAsync(ct);
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { break; }
            catch (Exception e) { _log.LogWarning(e, "Heartbeat tick failed"); }

            if (!await DelayAsync(AgentConfig.HeartbeatSeconds, ct)) break;
        }
    }

    // ------------------------------------------------------------------ commands

    /// <summary>Poll, claim, dispatch. Its cadence follows the operator: fast while a shell
    /// submission is in flight or something arrived recently, idle otherwise.</summary>
    private async Task CommandLoopAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try
            {
                if (await EnsureEnrolledAsync(ct))
                    await PollAndDispatchAsync(ct);
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { break; }
            catch (Exception e) { _log.LogWarning(e, "Command poll failed"); }

            // Fast while the operator is mid-something: a live shell submission waiting on
            // shell_input, or anything at all having arrived within CommandBurstSeconds.
            var busy = _shells.AnyActiveSubmission
                       || (DateTime.UtcNow - _lastCommandUtc).TotalSeconds < AgentConfig.CommandBurstSeconds;
            var every = busy ? AgentConfig.CommandPollFastSeconds : AgentConfig.CommandPollSeconds;
            if (!await DelayAsync(every, ct)) break;
        }
    }

    private async Task PollAndDispatchAsync(CancellationToken ct)
    {
        var commands = await _fleet.PollCommandsAsync(ct);
        if (commands.Count > 0) _lastCommandUtc = DateTime.UtcNow;

        foreach (var cmd in commands)
        {
            // Commands run on their own tasks, never inline: a run_script can take its full
            // 600s timeout, and awaiting it here would stop this loop claiming anything else
            // for that long -- including the shell_input that is trying to answer its prompt.
            //
            // Session-control commands (shell_input/signal/reset) steer a shell that is
            // ALREADY running a submission -- refusing them for concurrency would deadlock the
            // very command holding a slot. They're near-instant, so let them straight through.
            // shell_open joins them for a different reason: it is the one command that
            // runs for as long as an operator keeps a tab open, so counting it toward the
            // cap would let four idle terminals block every script and deployment on this
            // machine. Its own cap is MaxPtySessions (see PtySessionManager).
            bool isControl = cmd.Type is "shell_input" or "shell_signal" or "shell_reset"
                                      or "shell_open";
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

    // ------------------------------------------------------------------ inventory

    /// <summary>
    /// The slow local scans, on a loop of their own.
    ///
    /// All four are synchronous and genuinely expensive: profile discovery reads the
    /// registry and may mount a logged-off user's hive, the remote inventory enumerates
    /// WTS sessions and probes display outputs through PnP, the firmware read connects a
    /// vendor WMI namespace and enumerates a few hundred attributes, and the network scan
    /// walks every adapter plus two WMI classes and the NIC class registry key. They
    /// self-throttle (hourly, per-minute, six-hourly and quarter-hourly respectively) but
    /// when they DO run they take real time, and they used to run on the same loop as the
    /// heartbeat -- immediately in front of it. "Off the heartbeat path" is only true now
    /// that they are on a different thread from it.
    ///
    /// None posts anything itself; each leaves a payload for the next heartbeat to carry.
    /// </summary>
    private async Task InventoryLoopAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try
            {
                TempMonitorAgent.Backup.BackupProfileReporter.RefreshIfDue();
                TempMonitorAgent.Remote.RemoteInventoryReporter.RefreshIfDue();
                TempMonitorAgent.Bios.BiosInventoryReporter.RefreshIfDue();
                TempMonitorAgent.Network.NetworkInventoryReporter.RefreshIfDue();
            }
            catch (Exception e) { _log.LogWarning(e, "Inventory scan failed"); }

            if (!await DelayAsync(AgentConfig.InventoryScanSeconds, ct)) break;
        }
    }

    // ------------------------------------------------------------------ processes

    /// <summary>
    /// The live process list for the machine Processes card.
    ///
    /// **Idle by default, and that is the whole design.** Sampling enumerates every process
    /// on the machine, opens each one's token to resolve its owner, and asks WMI which of
    /// them host services. Doing that on a five-second cadence on every PC in the fleet, to
    /// answer a question being asked about one of them, would be pure waste -- so the hub
    /// sets `processes_wanted` on the heartbeat reply only while somebody has that card
    /// open, and this loop does nothing at all until it does.
    ///
    /// **It sends its own heartbeat rather than waiting for one.** The heartbeat loop is on
    /// a 10-second tick and a sample lands every 5, so half of them would arrive stale (or
    /// be dropped by the next sample) if this only left the payload behind. Calling
    /// HeartbeatAsync here is safe: FleetClient's HttpClient is built for concurrent use and
    /// every reporter hands its payload over under a lock, so the two callers cannot send
    /// the same inventory twice or interleave into one body.
    ///
    /// Separate from the inventory loop despite being another local scan, because those
    /// self-throttle to minutes and this one's entire value is being current.
    /// </summary>
    private async Task ProcessLoopAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            var watching = false;
            try
            {
                watching = ProcessReporter.Wanted;
                if (watching && _fleet.IsEnrolled && await ProcessReporter.SampleAsync(ct))
                    await _fleet.HeartbeatAsync(ct);
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { break; }
            catch (Exception e) { _log.LogWarning(e, "Process sample failed"); }

            // Fast enough to notice a card being opened without the operator feeling it, and
            // never faster than the sampling cadence once one is.
            var every = watching ? AgentConfig.ProcessSampleSeconds
                                 : AgentConfig.ProcessIdleCheckSeconds;
            if (!await DelayAsync(every, ct)) break;
        }
    }

    // ------------------------------------------------------------------ self-update

    /// <summary>
    /// The signed self-update check.
    ///
    /// Never swaps the binary out from under a running command: applying an update exits
    /// the process (code 17) for the SCM to restart, which would kill a half-finished script
    /// and report nothing back. Deferring costs at most one command's runtime on a weekly
    /// check, and the flag is left set so the next tick retries.
    ///
    /// Open TERMINALS are excluded from that count, deliberately. A shell_open command runs
    /// for as long as an operator keeps a tab open -- which can be days -- so counting it
    /// would let one forgotten tab stall this agent's updates indefinitely, and "the fleet
    /// stopped updating" is a much worse failure than "a terminal disconnected". The session
    /// ends cleanly either way: the runner reports "the agent is shutting down" and the
    /// console says so instead of hanging.
    /// </summary>
    private async Task UpdateLoopAsync(CancellationToken ct)
    {
        var lastCheck = DateTime.UtcNow;

        while (!ct.IsCancellationRequested)
        {
            // Checked often, acted on rarely: this only has to notice _updateDue flipping,
            // which the telemetry loop does the moment the hub advertises a newer version.
            if (!await DelayAsync(AgentConfig.IntervalSeconds, ct)) break;

            try
            {
                var due = _updateDue
                          || (DateTime.UtcNow - lastCheck).TotalSeconds >= AgentConfig.UpdateIntervalSeconds;
                if (!due) continue;

                var realWork = _running.Count - _ptys.OpenCount;
                if (realWork > 0)
                {
                    _log.LogInformation("Update deferred: {N} command(s) still running", realWork);
                    _updateDue = true;
                    continue;
                }

                _updateDue = false;
                lastCheck = DateTime.UtcNow;
                if (await _updater.CheckAndApplyAsync(ct)) { Restart(); return; }
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { break; }
            catch (Exception e) { _log.LogWarning(e, "Update check failed"); }
        }
    }

    // ------------------------------------------------------------------ shared

    /// <summary>Enroll if we haven't, at most once at a time. Returns whether the agent is
    /// enrolled; the loops simply skip their hub call when it isn't and try again next
    /// tick, which is what keeps a telemetry-only machine harmless.</summary>
    private async Task<bool> EnsureEnrolledAsync(CancellationToken ct)
    {
        if (_fleet.IsEnrolled) return true;

        // No secret means enrollment can never succeed -- this machine runs telemetry-only
        // until the installer is re-run. Worth saying, but exactly once: two loops asking
        // every ten seconds would otherwise fill the log with it.
        if (string.IsNullOrEmpty(_enrollmentSecret))
        {
            if (!_telemetryOnlyLogged)
            {
                _telemetryOnlyLogged = true;
                _log.LogWarning(
                    "No enrollment secret available; running telemetry-only (no fleet channel)");
            }
            return false;
        }

        await _enrollGate.WaitAsync(ct);
        try
        {
            // Re-check inside the gate: the other loop may have enrolled us while we waited.
            if (_fleet.IsEnrolled) return true;
            // ...and back off between attempts, so a secret the hub rejects is retried on a
            // human timescale rather than once per tick from each loop that wants a channel.
            if ((DateTime.UtcNow - _lastEnrollAttemptUtc).TotalSeconds < EnrollRetrySeconds)
                return false;

            _lastEnrollAttemptUtc = DateTime.UtcNow;
            return await _fleet.EnsureEnrolledAsync(_enrollmentSecret, ct);
        }
        finally
        {
            _enrollGate.Release();
        }
    }

    /// <summary>Sleep between ticks. Returns false when the host is stopping, so every loop
    /// exits the same way and none of them has to catch cancellation around its own delay.</summary>
    private static async Task<bool> DelayAsync(int seconds, CancellationToken ct)
    {
        try
        {
            await Task.Delay(TimeSpan.FromSeconds(seconds), ct);
            return true;
        }
        catch (OperationCanceledException) { return false; }
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
