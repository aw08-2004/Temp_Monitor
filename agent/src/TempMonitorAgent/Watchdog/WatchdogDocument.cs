using System.Text.Json.Nodes;
using System.Text.Json.Serialization;

namespace TempMonitorAgent.Watchdog;

/// <summary>
/// The watchdogs this machine has been told to keep, exactly as the hub resolved them
/// (roadmap #20).
///
/// **This is a desired state, not a condition**, and the absence of anything evaluable is the
/// design rather than a simplification. The hub's `watchdogs.py` module docstring gives the
/// argument in full; the part that matters on this side is that there is no expression
/// language here to disagree with the hub's, because there is no expression.
///
/// **It is persisted, and it does not expire.** `policy`'s device document carries a
/// max_age_seconds dead-man switch so that a phone whose hub is gone stops being a brick.
/// A watchdog is the reverse case: an unreachable hub is exactly when local evaluation earns
/// its keep, so the last document received stays in force indefinitely. The only way to stop
/// a watchdog is to reach the machine.
/// </summary>
public sealed record WatchdogDocument
{
    /// <summary>The hub's content hash for this document. Echoed on every heartbeat so the
    /// hub can skip re-sending an unchanged one -- the same shape RuntimeConfig.ConfigVersion
    /// uses. Empty means "never received one", which is what makes the first heartbeat fetch
    /// it.</summary>
    public string Version { get; init; } = "";

    public IReadOnlyList<WatchdogEntry> Entries { get; init; } = [];

    public static WatchdogDocument Empty { get; } = new();

    /// <summary>Read a document off a heartbeat reply. Returns null when the reply carried
    /// none, so a caller can tell "the hub had nothing to say" from "the hub said: nothing".
    ///
    /// **An entry with a missing or unusable field is DROPPED, not defaulted.** A watchdog
    /// with a grace of zero because the field did not parse is a watchdog that fights every
    /// installer on the machine, and it would do so silently. Dropping it costs one watchdog
    /// and shows up on the console as a machine that is not reporting on it.</summary>
    public static WatchdogDocument? Parse(JsonNode? root, string version)
    {
        if (root is null) return null;
        var array = root["watchdogs"]?.AsArray();
        if (array is null) return null;

        var entries = new List<WatchdogEntry>();
        foreach (var node in array)
        {
            var entry = WatchdogEntry.Parse(node);
            if (entry is not null) entries.Add(entry);
        }
        return new WatchdogDocument { Version = version, Entries = entries };
    }
}

/// <summary>One service this machine must keep running, and the limits on how hard to try.</summary>
public sealed record WatchdogEntry
{
    /// <summary>The hub's watchdog id. Carried back in every report, because the hub keys its
    /// state and its alert episodes on it -- never on the service name, which two watchdogs
    /// may legitimately share.</summary>
    public int Id { get; init; }

    /// <summary>The Windows service name (not the display name).</summary>
    public string Service { get; init; } = "";

    /// <summary>How long the service must be seen NOT running before anything is done. Stops
    /// the watchdog fighting an installer that stops a service for half a minute, which is
    /// the one way this feature could make a machine worse rather than better.</summary>
    public int GraceSeconds { get; init; }

    /// <summary>How many restarts inside <see cref="WindowSeconds"/> before standing down.</summary>
    public int MaxRestarts { get; init; }

    /// <summary>The window those restarts are counted over.</summary>
    public int WindowSeconds { get; init; }

    [JsonIgnore]
    public TimeSpan Grace => TimeSpan.FromSeconds(GraceSeconds);

    [JsonIgnore]
    public TimeSpan Window => TimeSpan.FromSeconds(WindowSeconds);

    public static WatchdogEntry? Parse(JsonNode? node)
    {
        try
        {
            var service = node?["service"]?.GetValue<string>();
            if (string.IsNullOrWhiteSpace(service)) return null;
            var id = node?["id"]?.GetValue<int>() ?? 0;
            if (id <= 0) return null;
            var grace = node?["grace_seconds"]?.GetValue<int>() ?? -1;
            var max = node?["max_restarts"]?.GetValue<int>() ?? 0;
            var window = node?["window_seconds"]?.GetValue<int>() ?? 0;
            // Bounds checked HERE as well as on the hub, on the same reasoning ProcessGuard
            // gives for keeping its own copy of the protected list: this is the side that
            // acts. A zero window would make the flap counter meaningless and a zero limit
            // would stand the watchdog down before it ever tried.
            if (grace < 0 || max < 1 || window < 1) return null;
            return new WatchdogEntry
            {
                Id = id,
                Service = service.Trim(),
                GraceSeconds = grace,
                MaxRestarts = max,
                WindowSeconds = window,
            };
        }
        catch (Exception e) when (e is InvalidOperationException or FormatException
                                       or OverflowException)
        {
            // A field of the wrong JSON type. Dropped, per the type docs above.
            return null;
        }
    }
}
