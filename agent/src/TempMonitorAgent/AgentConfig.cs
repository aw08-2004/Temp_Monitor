namespace TempMonitorAgent;

/// <summary>
/// Static configuration for the agent: versions, intervals, endpoints, embedded
/// trust roots, and %ProgramData% state paths. Mirrors the constants at the top of
/// companion.py so the wire behaviour stays in parity.
/// </summary>
public static class AgentConfig
{
    /// <summary>Reported to the hub as companion_version; also the self-update baseline.
    /// MUST match &lt;Version&gt; in TempMonitorAgent.csproj.</summary>
    public const string Version = "3.12.0";

    /// <summary>Reads a FLEETHUB_* setting, falling back to the pre-rename TEMP_MONITOR_*
    /// name. Machines installed before the FleetHub rename still have the old machine-level
    /// env vars set, and an agent that self-updates onto a new build must keep honouring
    /// them -- otherwise a box pinned at a non-default hub silently swings back to the
    /// production default. Drop the fallback only once no TEMP_MONITOR_* vars remain in
    /// the field.</summary>
    private static string? Env(string name, string legacyName)
    {
        var v = Environment.GetEnvironmentVariable("FLEETHUB_" + name);
        if (!string.IsNullOrEmpty(v)) return v;
        v = Environment.GetEnvironmentVariable("TEMP_MONITOR_" + legacyName);
        return string.IsNullOrEmpty(v) ? null : v;
    }

    // --- Hub endpoints -----------------------------------------------------
    // Base URL is overridable via FLEETHUB_HUB (legacy: TEMP_MONITOR_HUB) for local
    // testing (e.g. http://localhost:3001). Production default matches companion.py.
    public static string HubBase =>
        (Env("HUB", "HUB") ?? "https://temp.arkeanos.net").TrimEnd('/');

    public static string ReportUrl => HubBase + "/api/report";
    public static string EnrollUrl => HubBase + "/api/agent/enroll";
    public static string HeartbeatUrl => HubBase + "/api/agent/heartbeat";
    public static string CommandsUrl => HubBase + "/api/agent/commands";
    public static string CommandResultUrl(string commandId) =>
        HubBase + "/api/agent/commands/" + Uri.EscapeDataString(commandId) + "/result";
    public static string CommandOutputUrl(string commandId) =>
        HubBase + "/api/agent/commands/" + Uri.EscapeDataString(commandId) + "/output";

    /// <summary>Machine identity sent to the hub (the "machine" field).</summary>
    public static string MachineName =>
        Env("MACHINE", "MACHINE") is { Length: > 0 } n ? n : Environment.MachineName;

    // --- Cadence (seconds) -------------------------------------------------
    public const int IntervalSeconds = 5;         // main loop tick / temp report
    public const int SensorIntervalSeconds = 10;  // full sensor block
    public const int UptimeIntervalSeconds = 600; // uptime field
    public const int CommandPollSeconds = 10;     // poll + heartbeat (well under 90s online window)
    public const int UpdateIntervalSeconds = 7 * 24 * 60 * 60; // weekly self-update check

    public const int OfflineBufferMax = 1000;
    public const int MaxChainRestarts = 3;

    // --- Live command output streaming -------------------------------------
    // Flush every 1.5s (or sooner if the buffer fills). The console polls output at
    // ~1s, so this keeps perceived latency under ~2.5s while stopping a chatty script
    // from turning into one POST per line.
    public const int StreamFlushMillis = 1500;
    // Matches fleet.STREAM_MAX_CHUNK_CHARS: the hub rejects a bigger chunk, so split
    // before we get there.
    public const int StreamMaxChunkChars = 16_000;
    public const int StreamPostRetries = 3;

    // Commands execute off the main loop (see Worker), so a long script no longer
    // blocks telemetry/heartbeats. Bound the concurrency so a queued pile of scripts
    // can't exhaust the box. Session-control commands (shell_input/signal/reset) are
    // exempt -- they must reach a running submission even when this cap is hit.
    public const int MaxConcurrentCommands = 4;

    // --- Interactive shell sessions ----------------------------------------
    // One persistent shell per (operator, shell type). Reaped after this much idle time
    // with no in-flight submission, and capped so a crowd of operators can't spawn
    // unbounded SYSTEM shells. Default per-submission timeout when the console doesn't
    // send params.timeout_seconds.
    public const int ShellIdleTimeoutSeconds = 30 * 60;
    public const int MaxShellSessions = 8;
    public const int ShellDefaultTimeoutSeconds = 600;

    // While a submission is in flight, poll the command channel this fast so typed
    // shell_input reaches the shell promptly instead of waiting out CommandPollSeconds.
    public const int CommandPollFastSeconds = 1;

    // Exit code the service returns to request an SCM-driven restart onto a
    // freshly swapped binary (installer configures `sc failure ... restart`).
    public const int RestartExitCode = 17;

    // --- Trust roots -------------------------------------------------------
    // Fleet COMMANDS are not signed: the hub authorizes them on an allow-listed
    // console session and records them in its audit_log. (There was a
    // COMMAND_SIGNING_PUBLIC_KEY_HEX here; it was never populated, so in practice
    // every high-risk command was refused before this was removed.)
    //
    // The UPDATE trust root below is separate and still fully enforced.

    // Ed25519 public key that verifies the signed self-update manifest. Reuses
    // the existing companion update key (UPDATE_PUBLIC_KEY_HEX in companion.py).
    public const string UpdatePublicKeyHex =
        "9a4f433e0eb82fae121fdeede7d2ce881d50bc80021236f24fdfa4494fc0537c";

    // Signed update manifest served from the repo's main branch, plus its
    // detached signature. The actual binary lives at the URL inside the manifest.
    // Overridable via env (mirrors HubBase) so the self-update path can be exercised
    // against a local server instead of raw.githubusercontent.com.
    //
    // Deliberately still the pre-rename repo path. Pointing this at .../FleetHub/ before
    // the repo is actually renamed would 404 every update check -- and since a failed
    // check is (correctly) non-fatal and silent, the whole fleet would simply stop
    // updating with no way to push a fix. The old path works today and keeps working
    // after the rename via GitHub's redirect, so it is strictly the safer of the two.
    // Move it to the new path only in the release that follows the repo rename.
    public static string UpdateManifestUrl =>
        Env("UPDATE_MANIFEST_URL", "UPDATE_MANIFEST_URL")
        ?? "https://raw.githubusercontent.com/aw08-2004/Temp_Monitor/main/agent/agent.manifest.json";
    public static string UpdateManifestSigUrl => UpdateManifestUrl + ".sig";

    /// <summary>Honours FLEETHUB_NO_UPDATE, falling back to TEMP_MONITOR_NO_UPDATE.</summary>
    public static bool UpdatesDisabled => Env("NO_UPDATE", "NO_UPDATE") == "1";

    // --- State paths -------------------------------------------------------
    // New installs use %ProgramData%\FleetHub\Agent. Pre-rename installs are carried over
    // on first boot by MigrateAndResolve() below.
    //
    // This fallback is load-bearing: agent.json holds the machine identity the hub keys
    // on, so an agent that self-updates and can't find it re-enrols as a NEW machine and
    // duplicates the fleet. Resolve once at startup -- the answer must not change
    // mid-process, or a migration racing a write would split state across both dirs.
    // Migration runs inside the resolver, not as a separate startup step, so that the
    // very first touch of ProgramDataDir (Program.cs creates the log dir before the
    // logger exists) already sees the final answer. Splitting the two would let this
    // process resolve to the legacy dir, migrate, and then keep writing there -- and
    // those writes would be silently discarded on the next boot.
    private static readonly string _programDataDir = MigrateAndResolve();
    public static string ProgramDataDir => _programDataDir;

    /// <summary>Set by the resolver; drained by the caller once logging is up.</summary>
    private static string? _migrationNote;
    public static string? TakeMigrationNote()
    {
        var note = _migrationNote;
        _migrationNote = null;
        return note;
    }

    private static string CommonAppData =>
        Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData);

    internal static string NewProgramDataDir => Path.Combine(CommonAppData, "FleetHub", "Agent");
    internal static string LegacyProgramDataDir => Path.Combine(CommonAppData, "TempMonitorAgent");

    private static string MigrateAndResolve()
    {
        // Already on the new layout (or a clean install): nothing to carry over.
        if (Directory.Exists(NewProgramDataDir)) return NewProgramDataDir;
        if (!Directory.Exists(LegacyProgramDataDir)) return NewProgramDataDir;

        // Pre-rename install: copy state across, then run from the new dir. Copy rather
        // than move, deliberately -- a rollback to a pre-rename build must still find its
        // identity in the old location.
        try
        {
            Directory.CreateDirectory(NewProgramDataDir);
            foreach (var name in new[] { "agent.json", "config.json", "restart_state.json" })
            {
                var src = Path.Combine(LegacyProgramDataDir, name);
                if (File.Exists(src))
                    File.Copy(src, Path.Combine(NewProgramDataDir, name), overwrite: true);
            }
            _migrationNote = $"Migrated agent state {LegacyProgramDataDir} -> {NewProgramDataDir}";
            return NewProgramDataDir;
        }
        catch (Exception e)
        {
            // Never fatal, and never half-migrated: on any failure stay wholly on the
            // legacy dir. Losing agent.json would make this agent re-enrol as a new
            // machine, which is worse than running from the old path a while longer.
            _migrationNote = $"State migration failed, continuing on {LegacyProgramDataDir}: {e.Message}";
            return LegacyProgramDataDir;
        }
    }

    public static string AgentIdentityPath => Path.Combine(ProgramDataDir, "agent.json");
    /// <summary>Hub-delivered operational config (see RuntimeConfig). Persisted so a
    /// restart or self-update doesn't fall back to compiled defaults until the next
    /// heartbeat.</summary>
    public static string AgentConfigPath => Path.Combine(ProgramDataDir, "config.json");
    public static string RestartStatePath => Path.Combine(ProgramDataDir, "restart_state.json");
    public static string LogPath => Path.Combine(ProgramDataDir, "companion.log");
    public static string UpdateStagingDir => Path.Combine(ProgramDataDir, "update");

    // Registry location the installer writes the one-time enrollment secret to.
    // Pre-rename installers wrote it to the legacy key; an agent that self-updates onto
    // this build must still be able to authenticate, so both are read (new first).
    public const string RegistryKeyPath = @"SOFTWARE\FleetHub\Agent";
    public const string LegacyRegistryKeyPath = @"SOFTWARE\TempMonitorAgent";
    public const string RegistryEnrollmentSecretValue = "EnrollmentSecret";
}
