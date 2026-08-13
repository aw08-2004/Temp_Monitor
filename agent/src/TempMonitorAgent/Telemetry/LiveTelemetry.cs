namespace TempMonitorAgent.Telemetry;

/// <summary>
/// Whether an operator has this machine's page open, and therefore how fast this machine
/// should be reporting.
///
/// **The cadence follows attention.** Ordinarily telemetry goes out every
/// <see cref="AgentConfig.IntervalSeconds"/> with a full sensor block on every second one
/// -- which is about a dozen points a minute on a page whose charts are sixty seconds wide,
/// so a three-second spike is one dot or none. While the hub says somebody is watching
/// (<c>live_wanted</c> on the heartbeat, and on the watch poll beside it), the loop drops to
/// <see cref="AgentConfig.LiveIntervalSeconds"/> and carries sensors on EVERY tick, because
/// the load/power/fan panels are drawn from that block and a temperature moving at 1 Hz
/// beside eleven panels stepping every ten seconds is worse than not speeding up at all.
///
/// **Same shape and same reasoning as <see cref="ProcessReporter"/>'s watch**, deliberately:
/// demand-driven, one flag, hub-authoritative, and false whenever the hub has not said
/// otherwise -- including against a hub too old to have an opinion, which is exactly the
/// cadence those hubs already expect.
///
/// The hub also sends the interval it wants rather than letting this build assume one, so a
/// hub can dial it back for a fleet on a metered link without every agent needing an update.
/// It is clamped here: this process decides what it is willing to do to its own network.
/// </summary>
public static class LiveTelemetry
{
    /// <summary>Bounds on the hub-supplied cadence. The floor is a second because that is
    /// what the fast path is FOR, and a hub asking for 100 ms would be asking this machine to
    /// spend a core on sensor reads. The ceiling is the ordinary cadence: a hub asking for
    /// slower than normal is asking for nothing, and should say so with the flag instead.</summary>
    private const int MinIntervalSeconds = 1;

    private static readonly Lock Gate = new();
    private static bool _wanted;
    private static int _intervalSeconds = AgentConfig.LiveIntervalSeconds;

    /// <summary>Is somebody watching this machine's charts? Set from the hub's replies.</summary>
    public static bool Wanted
    {
        get { lock (Gate) return _wanted; }
    }

    /// <summary>The cadence to report at right now, in seconds: the fast one while watched,
    /// the ordinary one otherwise. One property so the telemetry loop has a single question
    /// to ask and cannot end up half-switched.</summary>
    public static int IntervalSeconds
    {
        get
        {
            lock (Gate) return _wanted ? _intervalSeconds : AgentConfig.IntervalSeconds;
        }
    }

    /// <summary>Apply the hub's answer. <paramref name="intervalSeconds"/> is what the hub
    /// asked for, or null when it did not say -- an older hub, or the flag arriving on its
    /// own -- in which case this build's own default stands.</summary>
    public static void SetWanted(bool wanted, int? intervalSeconds = null)
    {
        lock (Gate)
        {
            _wanted = wanted;
            if (intervalSeconds is int seconds)
            {
                _intervalSeconds = Math.Clamp(seconds, MinIntervalSeconds,
                                              AgentConfig.IntervalSeconds);
            }
        }
    }
}
