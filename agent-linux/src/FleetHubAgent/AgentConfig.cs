namespace FleetHubAgent;

/// <summary>
/// Static configuration for the Linux agent: version, hub endpoints, cadence, and the
/// state paths under /var/lib.
///
/// This is the Windows agent's AgentConfig with everything Windows-shaped removed and one
/// thing deliberately absent -- see <see cref="Version"/> for why there is no update
/// manifest here. Read that note before adding one.
///
/// **The wire protocol is not re-negotiated.** Every field name, header and route below is
/// the one the C# Windows agent already sends, because the hub is the same hub: a Linux box
/// enrolls through /api/agent/enroll, reports through /api/report and polls the same command
/// channel. Nothing on the hub had to change to accept this agent, which is the property
/// that let it be written at all -- see agent-linux/README.md.
/// </summary>
public static class AgentConfig
{
    /// <summary>Reported to the hub as companion_version, exactly like the Windows agent.
    /// MUST match &lt;Version&gt; in FleetHubAgent.csproj.
    ///
    /// **Starting at 0.x is a safety mechanism, not modesty.** The hub's
    /// AGENT_TRAIN_MIN_VERSION is "3.0.0", and three separate things key off being under it:
    ///
    ///   * get_advertised_version() returns nothing for a sub-3.0 reporter, so /api/report's
    ///     reply carries no latest_version -- the hub never points this agent at the WINDOWS
    ///     agent's signed manifest, which is a win-x64 binary it would have no idea what to
    ///     do with. That is the failure this number prevents.
    ///   * every MIN_*_AGENT gate in hub/static/js (MIN_PTY_AGENT, MIN_PROCESS_AGENT,
    ///     MIN_FILES_AGENT, MIN_OPEN_AGENT and friends) reads 0.1.0 as too old, so the
    ///     console does not offer a Linux machine a terminal, a process list or a file
    ///     browser that this agent cannot answer. The compatibility API the Windows agent's
    ///     MINOR feeds (see CLAUDE.md) does the right thing here for free.
    ///   * the dashboard's agents_outdated tally skips sub-3.0 machines, so a fleet of Linux
    ///     boxes does not permanently read as "behind" on a release train they are not on.
    ///
    /// So: this is a FOURTH version line, and it must stay under 3.0.0 until this agent
    /// genuinely implements the features those gates protect. Rejected alternative: starting
    /// at 3.35.0 to "match" the Windows agent. That reads to the hub as a fully-featured
    /// agent, and the console would immediately offer a ConPTY terminal to a machine with no
    /// ConPTY -- every one of those gates would pass on a lie.</summary>
    public const string Version = "0.1.0";

    /// <summary>Reads a FLEETHUB_* setting. No TEMP_MONITOR_* fallback, unlike the Windows
    /// agent: that fallback exists for machines installed before the FleetHub rename, and
    /// no Linux machine predates this file.</summary>
    private static string? Env(string name)
    {
        var v = Environment.GetEnvironmentVariable("FLEETHUB_" + name);
        return string.IsNullOrEmpty(v) ? null : v;
    }

    // --- Hub endpoints -----------------------------------------------------
    public static string HubBase =>
        (Env("HUB") ?? "https://temp.arkeanos.net").TrimEnd('/');

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
    /// Nothing calls it yet (the payload-fetching executors it guards are not ported). It is
    /// here so that the first thing to need it finds it, rather than reinventing the
    /// StartsWith version.
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

    /// <summary>Machine identity sent to the hub (the "machine" field). Environment.MachineName
    /// is the hostname up to the first dot, which is what the Windows fleet sends and what
    /// the hub keys on -- an FQDN here would enroll the same box twice if its resolv.conf
    /// ever changed.</summary>
    public static string MachineName =>
        Env("MACHINE") is { Length: > 0 } n ? n : Environment.MachineName;

    // --- Cadence (seconds) -------------------------------------------------
    // Same numbers as the Windows agent, and for the same reasons -- they are sized against
    // the HUB's clocks (the 90-second offline window, the console's own poll intervals), not
    // against anything about the operating system underneath. Diverging here would make a
    // Linux machine's console behave subtly differently for no reason anyone could name.
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
    public const int CommandPollFastSeconds = 1;

    public const int OfflineBufferMax = 1000;

    /// <summary>How often to check for a signed update. Weekly, matching the Windows agent.
    ///
    /// The Windows agent also gets an out-of-band nudge: /api/report answers it with
    /// `latest_version`, and it checks immediately on seeing a number ahead of its own. This
    /// agent gets no such hint and must not -- it reports a 0.x version, so the hub
    /// deliberately tells it nothing (see Version). A weekly poll is therefore the ONLY thing
    /// that moves a Linux machine onto a new build, which is worth knowing when timing a
    /// release: the fleet converges over a week, not over fifteen minutes.</summary>
    public const int UpdateIntervalSeconds = 7 * 24 * 60 * 60;

    /// <summary>How many times an update may chain-restart toward one target before the agent
    /// stops trying it. Three, as on Windows. This is what stops a build that starts, updates,
    /// crashes and starts again from burning a machine in a loop.</summary>
    public const int MaxChainRestarts = 3;

    /// <summary>Exit code used when leaving to come back on a freshly swapped binary.
    ///
    /// Nothing keys on the value: the unit is Restart=always, so any exit brings the agent
    /// back. It is distinct so the journal distinguishes a deliberate update restart from a
    /// crash -- systemd logs the code, and "exited with status 17" beside the updater's own
    /// line is the difference between reading that as planned or as a fault.</summary>
    public const int RestartExitCode = 17;

    /// <summary>Bound concurrent command execution so a queued pile of scripts cannot
    /// exhaust the box.</summary>
    public const int MaxConcurrentCommands = 4;

    /// <summary>Default per-command timeout when the console does not send one.</summary>
    public const int DefaultCommandTimeoutSeconds = 600;

    // --- Update trust root -------------------------------------------------

    /// <summary>Ed25519 public key that verifies the signed self-update manifest.
    ///
    /// **The same key the Windows fleet uses**, and that is the point rather than a shortcut:
    /// it is the release trust root for this whole product, held offline, and a second key
    /// would be a second thing to protect and a second way for a fleet to end up trusting
    /// something nobody meant it to. One key, two manifests -- see sign_release.py, whose
    /// --manifest flag is what lets it sign this one with no change to it.</summary>
    public const string UpdatePublicKeyHex =
        "9a4f433e0eb82fae121fdeede7d2ce881d50bc80021236f24fdfa4494fc0537c";

    /// <summary>The signed manifest, and its detached signature, read from `main`.
    ///
    /// Read from the BRANCH rather than from a release, exactly as the Windows agent does, and
    /// for the same reason: this is the one URL that must keep working for a fleet to be
    /// reachable at all, so it points at something that cannot be retagged, deleted or
    /// unpublished. The binary it names does live in a release; the manifest that authorises
    /// that binary does not.
    ///
    /// There is no beta channel here yet. The Windows agent picks between two manifests by a
    /// NAME the hub supplies, which keeps the hub out of the trust root; adding that before
    /// there is a second Linux build to put on it would be machinery with nothing to carry.
    ///
    /// Overridable via env (mirroring HubBase) so the update path can be exercised against a
    /// local server instead of raw.githubusercontent.com.</summary>
    public static string UpdateManifestUrl =>
        Env("UPDATE_MANIFEST_URL")
        ?? "https://raw.githubusercontent.com/aw08-2004/Temp_Monitor/main/agent-linux/agent.manifest.json";

    public static string UpdateManifestSigUrl => UpdateManifestUrl + ".sig";

    /// <summary>Honours FLEETHUB_NO_UPDATE=1, for a machine that must stay on a known build
    /// while something is being diagnosed on it.</summary>
    public static bool UpdatesDisabled => Env("NO_UPDATE") == "1";

    // --- State paths -------------------------------------------------------
    // /var/lib/fleethub/agent, not /etc and not /opt: this is variable state the agent
    // itself writes (the enrollment identity), which is exactly what /var/lib is for under
    // the FHS. There is no migration story here and no legacy directory to fall back to --
    // the Windows agent's MigrateAndResolve exists because that fleet was installed under a
    // different product name, and this one has never been installed at all.
    //
    // agent.json holds the machine identity the hub keys on, so an agent that cannot find it
    // re-enrolls as a NEW machine and duplicates the fleet. That is the failure this
    // directory's permissions (see StateDirectory) and the installer's ownership both exist
    // to prevent.
    private static readonly string _stateDir =
        Environment.GetEnvironmentVariable(StateDirOverrideVar) is { Length: > 0 } o
            ? o
            : "/var/lib/fleethub/agent";

    public static string StateDir => _stateDir;

    /// <summary>Test-only redirect for the state root, resolved once. Deliberately NOT
    /// routed through <see cref="Env"/>: a stray value here is the one setting that is
    /// unrecoverable, because the agent would not find agent.json and would duplicate this
    /// machine in the fleet. One explicit name, no aliases.</summary>
    internal const string StateDirOverrideVar = "FLEETHUB_STATE_DIR_FOR_TESTS";

    public static string AgentIdentityPath => Path.Combine(StateDir, "agent.json");

    /// <summary>The self-update restart guard. State, so it lives with agent.json under
    /// /var/lib -- unlike the update STAGING directory, which must sit beside the executable
    /// for the swap to be atomic. See SelfUpdater.StagingDir.</summary>
    public static string RestartStatePath => Path.Combine(StateDir, "restart_state.json");

    /// <summary>Where the installer drops the shared enrollment secret, when it is not
    /// supplied through the unit's EnvironmentFile instead.
    ///
    /// **This is the hub's AGENT_ENROLLMENT_SECRET, the same value on every machine in the
    /// fleet** -- not a per-machine credential, and not consumed by being used. What IS minted
    /// per machine, and returned exactly once, is the token enroll hands back (see
    /// fleet.enroll_agent). So a leak of this file is a leak of the fleet's enrollment
    /// credential rather than of one box's identity.
    ///
    /// 0600 root:root for that reason: a world-readable copy would let any local user enroll a
    /// machine of their choosing into this fleet. See Worker.ReadEnrollmentSecret, which
    /// refuses to read it rather than quietly accepting the wrong mode.</summary>
    public static string EnrollmentSecretPath => "/etc/fleethub/agent.secret";

    /// <summary>The env var the systemd unit's EnvironmentFile can carry the secret in,
    /// matching the name the Windows agent already honours.</summary>
    public const string EnrollmentSecretVar = "AGENT_ENROLLMENT_SECRET";
}
