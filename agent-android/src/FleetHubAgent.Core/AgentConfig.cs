namespace FleetHubAgent;

/// <summary>
/// Static configuration for the Android agent: version, hub endpoints, and cadence.
///
/// This is agent-linux's AgentConfig with the Linux-shaped parts removed, and it is meant to
/// be diffed against that file rather than read alone -- the cadence numbers, the hub URL
/// test and the version reasoning are deliberately identical, and a difference between the
/// two should be one of the ones commented below.
///
/// **The wire protocol is not re-negotiated.** Every field name, header and route is the one
/// the C# Windows agent already sends, because the hub is the same hub: a phone enrolls
/// through /api/agent/enroll, reports through /api/report and polls the same command channel.
/// Nothing on the hub had to change to accept this agent, which is the property that let it
/// be written at all -- see agent-android/README.md.
///
/// **Two things differ from every other agent, and both come from Android having no
/// environment.** There are no environment variables to read a hub URL out of and no /etc to
/// drop a secret in, so both arrive through <see cref="Configure"/> from the platform layer
/// (managed configuration, then the setup screen). See State/IStateStore.cs.
/// </summary>
public static class AgentConfig
{
    /// <summary>Reported to the hub as companion_version, exactly like every other agent.
    /// MUST match &lt;Version&gt; in both csproj files -- see VersionGateTests.
    ///
    /// **This is its own version line, and the hub knows that now** (roadmap #22). It used to
    /// be pinned under the hub's AGENT_TRAIN_MIN_VERSION ("3.0.0") as a safety mechanism,
    /// because three separate things read `companion_version` as though it could only ever
    /// mean the Windows agent. Two of the three are fixed:
    ///
    ///   * get_advertised_version() now picks a manifest by the PLATFORM this agent reports,
    ///     so it can never point a phone at the Windows agent's signed manifest. That matters
    ///     more here than on Linux: there the advertised artifact is merely a win-x64 binary
    ///     the machine cannot run, here it would be offered to a device that cannot execute a
    ///     native binary at all.
    ///   * the dashboard's agents_outdated tally compares each machine against its own train,
    ///     so a shelf of tablets no longer reads as permanently "behind".
    ///
    /// What is NOT yet fixed, and is the reason this still says 0.1.0: the MIN_*_AGENT gates
    /// in hub/static/js still read a version number, and 0.1.0 is what keeps the console from
    /// offering a phone a terminal, a process list or a file browser. Unlike on Linux those
    /// are not merely unimplemented -- the platform forbids them to an ordinary app -- so
    /// there is no future version of this agent that could answer them, which is exactly what
    /// a version number cannot say and a capability report can (see AgentCapabilities).
    ///
    /// Rejected alternative, and it is worth keeping: matching the Windows agent's number so
    /// the fleet "looks consistent". That reads to the hub as a fully-featured agent on
    /// somebody else's train, and every one of those gates would pass on a lie.</summary>
    public const string Version = "0.1.0";

    // --- Hub endpoints -----------------------------------------------------

    /// <summary>The compiled-in hub, used until <see cref="Configure"/> says otherwise. Same
    /// default as the Linux agent so a device installed with no configuration at all lands in
    /// the same place as every other machine.</summary>
    public const string DefaultHubBase = "https://temp.arkeanos.net";

    private static string _hubBase = DefaultHubBase;

    /// <summary>
    /// Point the agent at a hub. Called ONCE by the platform layer before any loop starts.
    ///
    /// A settable static is a smell and this one is worth defending. The Linux agent reads
    /// $FLEETHUB_HUB at first use, which is safe because a process's environment cannot change
    /// under it. Android has no environment: the hub URL comes from RestrictionsManager
    /// (an MDM's managed configuration) or from the setup screen, and neither is reachable
    /// from a project that must not reference Android. So the platform resolves it and pushes
    /// it in here, at startup, on one thread, before the loops exist.
    ///
    /// **A blank or unparseable value is ignored rather than accepted.** An agent pointed at
    /// "" would report nowhere and look identical, from the device, to one that is working.
    /// </summary>
    public static void Configure(string? hubBase)
    {
        var trimmed = (hubBase ?? "").Trim().TrimEnd('/');
        if (trimmed.Length == 0) return;
        if (!Uri.TryCreate(trimmed, UriKind.Absolute, out var parsed)) return;
        if (parsed.Scheme != Uri.UriSchemeHttps && parsed.Scheme != Uri.UriSchemeHttp) return;
        _hubBase = trimmed;
    }

    public static string HubBase => _hubBase;

    public static string ReportUrl => HubBase + "/api/report";
    public static string EnrollUrl => HubBase + "/api/agent/enroll";
    public static string HeartbeatUrl => HubBase + "/api/agent/heartbeat";
    public static string CommandsUrl => HubBase + "/api/agent/commands";

    /// <summary>The command endpoint, asking the hub to HOLD the request open for up to
    /// <paramref name="waitSeconds"/> if nothing is queued. The hub applies its own ceiling
    /// and may decline, which is why the response says whether it did -- see
    /// <see cref="CommandPollResult"/>.</summary>
    public static string CommandsUrl_Waiting(int waitSeconds) =>
        CommandsUrl + "?wait=" + waitSeconds.ToString(System.Globalization.CultureInfo.InvariantCulture);

    public static string CommandResultUrl(string commandId) =>
        HubBase + "/api/agent/commands/" + Uri.EscapeDataString(commandId) + "/result";

    /// <summary>
    /// Does <paramref name="url"/> actually address OUR hub?
    ///
    /// Carried over from the Windows agent unchanged, because the reasoning is not
    /// platform-specific: this is what decides whether a request carries the agent's bearer
    /// token, so it is answered on the parsed ORIGIN and never on the URL's spelling. A
    /// StartsWith(HubBase) test says yes to https://hub.example.com.attacker.net/x and to
    /// https://hub.example.com@attacker.net/x, whose real hosts are attacker.net in both
    /// cases -- either one hands this agent's credential to whoever supplied the URL.
    ///
    /// Nothing calls it yet, for the same reason as on Linux: the payload-fetching executors
    /// it guards are not ported. It is here so that the first thing to need it finds it,
    /// rather than reinventing the StartsWith version.
    /// </summary>
    public static bool IsHubUrl(string? url)
    {
        if (string.IsNullOrWhiteSpace(url)) return false;
        if (!Uri.TryCreate(url, UriKind.Absolute, out var target)) return false;
        if (!Uri.TryCreate(HubBase, UriKind.Absolute, out var hub)) return false;
        return string.Equals(target.Scheme, hub.Scheme, StringComparison.OrdinalIgnoreCase)
            && string.Equals(target.Host, hub.Host, StringComparison.OrdinalIgnoreCase)
            && target.Port == hub.Port;
    }

    // --- Cadence (seconds) -------------------------------------------------
    // The same numbers as every other agent, and for the same reason -- they are sized
    // against the HUB's clocks (the 90-second offline window, the console's own poll
    // intervals), not against anything about the operating system underneath.
    //
    // **The temptation to slow these down for a battery-powered device is real and is
    // deliberately not taken here.** Halving the heartbeat rate would not halve the battery
    // cost (the radio's tail time dominates, not the payload) and it WOULD change what
    // "offline" means for one class of machine in the console. If a phone's battery turns out
    // to be the binding constraint, the honest fix is a hub-side per-machine cadence the
    // console knows about -- not a second, quieter definition of online that only Android
    // machines follow. Recorded in ROADMAP.MD #23.
    public const int IntervalSeconds = 5;         // temp report
    public const int SensorIntervalSeconds = 10;  // full sensor block
    public const int UptimeIntervalSeconds = 600; // uptime field
    public const int HeartbeatSeconds = 10;       // liveness (well under the 90s online window)
    public const int CommandPollSeconds = 10;     // idle command poll (the fallback cadence)

    /// <summary>How long the agent asks the hub to hold an empty command request open.
    /// Deliberately shorter than the hub's own 60s maximum so the ceiling that ends the
    /// connection is ours, and well under the 90-second offline window -- the hub refreshes
    /// last_seen when the request ARRIVES, so a machine parked in a longer hold would read
    /// offline in the console while sitting there perfectly healthy.</summary>
    public const int CommandWaitSeconds = 25;

    /// <summary>Whole-request budget for a held poll: the hold plus room for a slow link.
    /// Its own HttpClient, because the shared 10-second one exists to catch a hub that has
    /// stopped answering -- and a request we ASKED to be answered late must not trip it.</summary>
    public const int CommandPollTimeoutSeconds = CommandWaitSeconds + 15;

    /// <summary>Keep polling fast for a short window after anything arrives: an operator
    /// doing something is rarely doing one thing.</summary>
    public const int CommandBurstSeconds = 20;

    /// <summary>How often the inventory loop asks each source whether it is DUE. Not how often
    /// anything is read: a source states its own refresh interval, in minutes or hours, and
    /// this tick is only the resolution at which those are noticed.
    ///
    /// Sixty seconds because that is also how quickly an Invalidate() takes effect. After an
    /// app policy is applied, the console must not show yesterday's installed list beside
    /// today's policy for the rest of a fifteen-minute interval -- and a minute of staleness on
    /// a list of installed apps is not something anybody notices.</summary>
    public const int InventoryTickSeconds = 60;
    public const int CommandPollFastSeconds = 1;

    public const int OfflineBufferMax = 1000;

    /// <summary>Bound concurrent command execution. Lower than the Linux agent's 4 because
    /// this agent's whole command set is short, local and non-blocking -- nothing here can
    /// occupy a slot for ten minutes the way run_script can, so a deeper pool would only
    /// widen the window in which two commands race over the same stored name.</summary>
    public const int MaxConcurrentCommands = 2;

    /// <summary>Default per-command timeout when the console does not send one.</summary>
    public const int DefaultCommandTimeoutSeconds = 600;
}
