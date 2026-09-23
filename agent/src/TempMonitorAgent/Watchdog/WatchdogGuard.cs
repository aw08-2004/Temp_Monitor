namespace TempMonitorAgent.Watchdog;

/// <summary>
/// Services this agent will never restart, whatever the hub sends.
///
/// **The hub keeps the same list and THIS copy is the authority**, which is precisely the
/// arrangement <see cref="Fleet.Executors.ProcessGuard"/> documents for `end_process`: the
/// hub's copy exists so an operator gets an immediate refusal in the console, and this one
/// exists because a refusal that only lives on the hub is a refusal one compromised or simply
/// older hub can skip. Extend both or neither.
///
/// Each entry is here for a reason, and the reason is the same shape every time: restarting it
/// does not recover the machine, it takes the machine down.
///
///   rpcss, dcomlaunch  -- the machine bugchecks, or the SCM forces a restart a minute later.
///                         Windows refuses the stop anyway; what this prevents is an agent
///                         spending its life on a refused SCM call every thirty seconds.
///   plugplay, power, brokerinfrastructure, systemeventsbroker, lsm
///                      -- the same class: critical, non-stoppable, and a dependency of most
///                         of what else is running.
///   winmgmt            -- stoppable, and the wrong answer every time. Restarting WMI takes
///                         every dependent with it, and this agent reads its own sensors
///                         through it, so a watchdog on WMI would restart the thing the
///                         watchdog is watching from.
///   gpsvc              -- Group Policy. Restarting it mid-apply is how a machine ends up with
///                         half a policy and nothing recording which half.
///   tempmonitoragent   -- ourselves. If this service is stopped, nothing is evaluating the
///                         watchdog, so a watchdog on it could never fire. Refusing says that
///                         out loud; accepting would look like protection and be nothing.
/// </summary>
public static class WatchdogGuard
{
    private static readonly HashSet<string> Protected = new(StringComparer.OrdinalIgnoreCase)
    {
        "rpcss", "dcomlaunch", "plugplay", "power", "brokerinfrastructure",
        "systemeventsbroker", "lsm", "winmgmt", "gpsvc", "tempmonitoragent",
    };

    /// <summary>Trimmed -- Windows service names are compared case-insensitively, which the
    /// set's comparer already handles, so this normalises only what an operator can type by
    /// accident.</summary>
    public static string Normalize(string? name) => (name ?? "").Trim();

    public static bool IsProtected(string? name) => Protected.Contains(Normalize(name));
}
