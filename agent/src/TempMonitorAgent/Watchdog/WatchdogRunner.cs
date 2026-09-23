using System.ServiceProcess;
using Microsoft.Extensions.Logging;
using TempMonitorAgent.Fleet;
using TempMonitorAgent.State;

namespace TempMonitorAgent.Watchdog;

/// <summary>
/// The agent-local half of roadmap #20: look at the services this machine has been told to
/// keep, and put back the ones that stopped.
///
/// **The whole point is that no round trip to the hub is involved.** A rule evaluated hub-side
/// runs on reported data on a 60-second evaluator tick, over a 10-second heartbeat, against a
/// 90-second offline window -- which is the wrong latency for "restart this within two
/// minutes" and is nothing at all on a machine that is briefly off the network. This loop runs
/// on the machine, every <see cref="TickSeconds"/> seconds, whether or not the hub is
/// reachable, and reports afterwards.
///
/// The state machine per watchdog, once a tick:
///
///   * Running, or starting     -> `ok`. The start-pending case matters: a service coming up
///                                 on its own must not be counted as stopped, or the grace
///                                 period would be spent racing it.
///   * Not installed            -> `missing`. Distinguished from a failure deliberately -- a
///                                 typo'd service name is an authoring mistake, not a machine
///                                 in trouble, and the hub does not raise an alert for it.
///   * Stopped, inside grace    -> nothing done, and the last reported status is KEPT. This is
///                                 the window in which an installer legitimately has the
///                                 service down, and reporting a state change for it would
///                                 turn every software update into an event on the console.
///   * Stopped, past grace, and
///     under the flap limit     -> restart it. `restarted` on success, `failed` if it did not
///                                 come back.
///   * Stopped, past grace, at
///     the flap limit           -> `given_up`. Nothing is attempted until the oldest restart
///                                 ages out of the window, at which point it tries again on
///                                 its own. That self-clearing is why the limit is a count in
///                                 a window rather than a latch somebody has to reset.
///
/// **The restart history is persisted**, and that is not an optimisation. A service that takes
/// the agent down with it -- or an agent self-update, or a reboot -- would otherwise reset the
/// counter, and a machine that flaps hard enough to restart the agent would restart its
/// service forever while the hub was told everything was fine.
///
/// Fails soft everywhere. A watchdog whose service cannot be queried costs one tick, never the
/// loop, because the loop going quiet is indistinguishable from a healthy machine.
/// </summary>
public sealed class WatchdogRunner(ILogger<WatchdogRunner> log, AgentState state,
                                   IServiceControl services)
{
    /// <summary>How often the services are looked at. With the default 60-second grace this
    /// puts a stopped service back inside about 90 seconds, which is what #20 asks for; making
    /// it much shorter would spend a machine's day on SCM queries for no gain.</summary>
    public const int TickSeconds = 30;

    private readonly ILogger<WatchdogRunner> _log = log;
    private readonly AgentState _state = state;
    private readonly IServiceControl _services = services;
    private readonly Lock _gate = new();

    private WatchdogDocument _document = WatchdogDocument.Empty;
    /// <summary>Per watchdog id, everything this loop remembers between ticks.</summary>
    private Dictionary<int, WatchdogLocalState> _local = new();
    private bool _loaded;

    /// <summary>The version of the document currently held, for the heartbeat to echo.</summary>
    public string Version
    {
        get { lock (_gate) { EnsureLoaded(); return _document.Version; } }
    }

    /// <summary>Adopt a document the hub just sent, and persist it.
    ///
    /// State for a watchdog that survived the edit is KEPT, including its restart history.
    /// Dropping it would hand an operator a way to clear a flap limit by touching an unrelated
    /// field of an unrelated watchdog, which is the sort of thing nobody discovers until the
    /// day it matters.</summary>
    public void Apply(WatchdogDocument document)
    {
        lock (_gate)
        {
            EnsureLoaded();
            _document = document;
            var kept = new Dictionary<int, WatchdogLocalState>();
            foreach (var entry in document.Entries)
            {
                kept[entry.Id] = _local.TryGetValue(entry.Id, out var existing)
                    ? existing
                    : new WatchdogLocalState();
            }
            _local = kept;
            Persist();
        }
        _log.LogInformation("Holding {Count} watchdogs (document {Version})",
                            document.Entries.Count, document.Version);
    }

    /// <summary>One pass over every watchdog. Called from the worker loop; safe to call from
    /// one thread only, which is how Worker drives it.</summary>
    public void Tick() => Tick(DateTimeOffset.UtcNow);

    /// <summary>The pass, with the clock injected so it can be tested without waiting a grace
    /// period in real time. The whole feature is about timing, so a test that cannot move the
    /// clock cannot test it at all.</summary>
    public void Tick(DateTimeOffset now)
    {
        WatchdogEntry[] entries;
        lock (_gate)
        {
            EnsureLoaded();
            entries = [.. _document.Entries];
        }
        if (entries.Length == 0) return;

        var changed = false;
        foreach (var entry in entries)
        {
            try
            {
                changed |= Evaluate(entry, now);
            }
            catch (Exception e)
            {
                // One watchdog must never cost the others their tick. Logged at warning
                // because reaching here means something threw that the paths below were
                // written to return instead.
                _log.LogWarning(e, "Watchdog {Id} ({Service}) failed its pass",
                                entry.Id, entry.Service);
            }
        }
        if (changed) lock (_gate) Persist();
    }

    /// <summary>Evaluate one watchdog. Returns true when something worth persisting moved.</summary>
    private bool Evaluate(WatchdogEntry entry, DateTimeOffset now)
    {
        WatchdogLocalState local;
        lock (_gate)
        {
            if (!_local.TryGetValue(entry.Id, out var found)) return false;
            local = found;
        }

        // Checked before the service is even queried. See WatchdogGuard: a hub asking for one
        // of these means the two lists have diverged, and that is a thing a human has to know
        // about, so it is reported as a failure rather than silently skipped.
        if (WatchdogGuard.IsProtected(entry.Service))
        {
            return Report(entry, local, WatchdogStatus.Failed,
                          $"this agent will not restart {entry.Service}: restarting it does "
                          + "not recover a machine, it takes it down");
        }

        var status = _services.StatusOf(entry.Service);
        if (status is null)
        {
            local.StoppedSinceUtc = null;
            return Report(entry, local, WatchdogStatus.Missing,
                          $"there is no {entry.Service} service on this machine");
        }

        // Running, or on its way there under its own steam. StartPending is the case that
        // earns the check: a service the SCM is already starting must not spend the grace
        // period being raced by this loop.
        if (status is ServiceControllerStatus.Running
                   or ServiceControllerStatus.StartPending
                   or ServiceControllerStatus.ContinuePending)
        {
            local.StoppedSinceUtc = null;
            return Report(entry, local, WatchdogStatus.Ok, "");
        }

        local.StoppedSinceUtc ??= now;
        if (now - local.StoppedSinceUtc.Value < entry.Grace)
        {
            // Inside the grace period: nothing is done and NOTHING IS REPORTED. An installer
            // stopping a service for forty seconds is the ordinary case, and turning it into a
            // console event would make the history unreadable within a week.
            return true;   // StoppedSinceUtc moved, so it is worth persisting
        }

        local.PruneRestarts(now, entry.Window);
        if (local.Restarts.Count >= entry.MaxRestarts)
        {
            // Standing down. Not a latch: the oldest restart ages out of the window on its
            // own, and the next tick after that tries again.
            var retryAt = local.Restarts[0] + entry.Window;
            return Report(entry, local, WatchdogStatus.GivenUp,
                          $"{entry.Service} has been restarted {local.Restarts.Count} times in "
                          + $"the last {(int)entry.Window.TotalMinutes} minutes and keeps "
                          + $"stopping; not trying again before {retryAt:u}");
        }

        _log.LogInformation("Watchdog {Id}: {Service} is stopped, restarting it",
                            entry.Id, entry.Service);
        var result = _services.Restart(entry.Service);
        if (result.Missing)
        {
            local.StoppedSinceUtc = null;
            return Report(entry, local, WatchdogStatus.Missing, result.Summary);
        }
        if (!result.Ok)
        {
            // The attempt counts toward the flap limit whether or not it worked. A service
            // that refuses to start would otherwise be retried every thirty seconds forever,
            // and the escalation -- the part a human reads -- would never arrive.
            local.Restarts.Add(now);
            return Report(entry, local, WatchdogStatus.Failed, result.Summary);
        }
        local.Restarts.Add(now);
        local.StoppedSinceUtc = null;
        return Report(entry, local, WatchdogStatus.Restarted, result.Summary);
    }

    /// <summary>Record a status for the next heartbeat. Returns true when anything moved.</summary>
    private bool Report(WatchdogEntry entry, WatchdogLocalState local, WatchdogStatus status,
                        string detail)
    {
        var restartCount = local.Restarts.Count;
        var lastRestart = local.Restarts.Count > 0
            ? local.Restarts[^1].ToUnixTimeSeconds()
            : (long?)null;
        var moved = local.Status != status || local.LastRestartUnix != lastRestart;
        local.Status = status;
        local.Detail = detail;
        local.LastRestartUnix = lastRestart;
        WatchdogReporter.Record(entry.Id, status, restartCount, lastRestart, detail);
        return moved || local.StoppedSinceUtc is not null;
    }

    // --- persistence -------------------------------------------------------

    /// <summary>Load the document and the restart history from disk, once per process.
    ///
    /// Called under the lock from every public entry point rather than from a constructor, so
    /// that a corrupt file costs the first tick rather than the service's startup. Failing to
    /// read it leaves the agent holding NO watchdogs, which is the safe direction: doing
    /// nothing is what this loop does when everything is well.</summary>
    private void EnsureLoaded()
    {
        if (_loaded) return;
        _loaded = true;
        var stored = _state.LoadWatchdogState();
        if (stored is null) return;
        _document = new WatchdogDocument
        {
            Version = stored.Version ?? "",
            Entries = stored.Entries ?? [],
        };
        _local = new Dictionary<int, WatchdogLocalState>();
        foreach (var entry in _document.Entries)
        {
            var found = (stored.Local ?? []).FirstOrDefault(s => s.Id == entry.Id);
            _local[entry.Id] = found is null
                ? new WatchdogLocalState()
                : WatchdogLocalState.FromStored(found);
        }
        _log.LogInformation("Restored {Count} watchdogs from disk (document {Version})",
                            _document.Entries.Count, _document.Version);
    }

    /// <summary>Write the document and the restart history back. Must be called under the
    /// lock.</summary>
    private void Persist()
    {
        var stored = new StoredWatchdogState
        {
            Version = _document.Version,
            Entries = [.. _document.Entries],
            Local = [.. _local.Select(pair => pair.Value.ToStored(pair.Key))],
        };
        _state.SaveWatchdogState(stored);
    }
}

/// <summary>What the runner remembers about one watchdog between ticks.</summary>
internal sealed class WatchdogLocalState
{
    /// <summary>When the service was first seen not running in the current outage, or null
    /// while it is up. Cleared on every healthy look, which is what makes the grace period
    /// measure THIS outage rather than the time since the last one.</summary>
    public DateTimeOffset? StoppedSinceUtc { get; set; }

    /// <summary>Restart attempts, oldest first, inside the flap window.</summary>
    public List<DateTimeOffset> Restarts { get; } = [];

    public WatchdogStatus Status { get; set; } = WatchdogStatus.Ok;
    public string Detail { get; set; } = "";
    public long? LastRestartUnix { get; set; }

    /// <summary>Drop restarts that have aged out. This is the whole of the flap limit's
    /// self-clearing: once the oldest attempt falls outside the window the count drops below
    /// the limit and the next tick tries again, with nobody having to reset anything.</summary>
    public void PruneRestarts(DateTimeOffset now, TimeSpan window)
    {
        Restarts.RemoveAll(at => now - at >= window);
    }

    public StoredWatchdogLocal ToStored(int id) => new()
    {
        Id = id,
        StoppedSinceUnix = StoppedSinceUtc?.ToUnixTimeSeconds(),
        RestartsUnix = [.. Restarts.Select(at => at.ToUnixTimeSeconds())],
        Status = Status.ToString(),
        Detail = Detail,
    };

    public static WatchdogLocalState FromStored(StoredWatchdogLocal stored)
    {
        var local = new WatchdogLocalState
        {
            StoppedSinceUtc = stored.StoppedSinceUnix is null
                ? null
                : DateTimeOffset.FromUnixTimeSeconds(stored.StoppedSinceUnix.Value),
            Detail = stored.Detail ?? "",
        };
        foreach (var at in stored.RestartsUnix ?? [])
            local.Restarts.Add(DateTimeOffset.FromUnixTimeSeconds(at));
        local.Restarts.Sort();
        if (local.Restarts.Count > 0)
            local.LastRestartUnix = local.Restarts[^1].ToUnixTimeSeconds();
        // A status this build does not recognise reads as Ok rather than throwing. The stored
        // file outlives a downgrade, and a service failing to start because its own state file
        // named a newer enum member would be the least useful failure this agent could have.
        local.Status = Enum.TryParse<WatchdogStatus>(stored.Status, out var parsed)
            ? parsed : WatchdogStatus.Ok;
        return local;
    }
}

/// <summary>The status vocabulary, mirroring hub `watchdogs.STATUSES`. The wire form is the
/// hub's snake_case spelling, produced by <see cref="WatchdogStatusExtensions.Wire"/> rather
/// than by ToString, so renaming a member here cannot silently change what the hub is told.</summary>
public enum WatchdogStatus
{
    Ok,
    Restarted,
    Failed,
    GivenUp,
    Missing,
}

public static class WatchdogStatusExtensions
{
    public static string Wire(this WatchdogStatus status) => status switch
    {
        WatchdogStatus.Ok => "ok",
        WatchdogStatus.Restarted => "restarted",
        WatchdogStatus.Failed => "failed",
        WatchdogStatus.GivenUp => "given_up",
        WatchdogStatus.Missing => "missing",
        _ => "ok",
    };
}

/// <summary>The on-disk shape of everything above. A plain DTO so System.Text.Json can round
/// trip it without the runner's types needing setters for the sake of serialisation.</summary>
public sealed class StoredWatchdogState
{
    public string? Version { get; set; }
    public List<WatchdogEntry>? Entries { get; set; }
    public List<StoredWatchdogLocal>? Local { get; set; }
}

public sealed class StoredWatchdogLocal
{
    public int Id { get; set; }
    public long? StoppedSinceUnix { get; set; }
    public List<long>? RestartsUnix { get; set; }
    public string? Status { get; set; }
    public string? Detail { get; set; }
}
