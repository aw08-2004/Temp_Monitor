namespace TempMonitorAgent;

/// <summary>
/// Static configuration for the agent: versions, intervals, endpoints, embedded
/// trust roots, and %ProgramData% state paths. Mirrors the constants at the top of
/// companion.py so the wire behaviour stays in parity.
/// </summary>
public static class AgentConfig
{
    /// <summary>Reported to the hub as companion_version; also the self-update baseline.</summary>
    public const string Version = "3.0.0";

    // --- Hub endpoints -----------------------------------------------------
    // Base URL is overridable via TEMP_MONITOR_HUB for local testing
    // (e.g. http://localhost:3001). Production default matches companion.py.
    public static string HubBase =>
        (Environment.GetEnvironmentVariable("TEMP_MONITOR_HUB") ?? "https://temp.arkeanos.net")
        .TrimEnd('/');

    public static string ReportUrl => HubBase + "/api/report";
    public static string EnrollUrl => HubBase + "/api/agent/enroll";
    public static string HeartbeatUrl => HubBase + "/api/agent/heartbeat";
    public static string CommandsUrl => HubBase + "/api/agent/commands";
    public static string CommandResultUrl(string commandId) =>
        HubBase + "/api/agent/commands/" + Uri.EscapeDataString(commandId) + "/result";

    /// <summary>Machine identity sent to the hub (the "machine" field).</summary>
    public static string MachineName =>
        Environment.GetEnvironmentVariable("TEMP_MONITOR_MACHINE") is { Length: > 0 } n
            ? n
            : Environment.MachineName;

    // --- Cadence (seconds) -------------------------------------------------
    public const int IntervalSeconds = 5;         // main loop tick / temp report
    public const int SensorIntervalSeconds = 10;  // full sensor block
    public const int UptimeIntervalSeconds = 600; // uptime field
    public const int CommandPollSeconds = 10;     // poll + heartbeat (well under 90s online window)
    public const int UpdateIntervalSeconds = 7 * 24 * 60 * 60; // weekly self-update check

    public const int OfflineBufferMax = 1000;
    public const int MaxChainRestarts = 3;

    // Exit code the service returns to request an SCM-driven restart onto a
    // freshly swapped binary (installer configures `sc failure ... restart`).
    public const int RestartExitCode = 17;

    // --- Trust roots -------------------------------------------------------
    // Ed25519 public key that verifies signed fleet commands. Must equal the
    // hub's COMMAND_SIGNING_PUBLIC_KEY_HEX. Baked into the binary for production;
    // overridable via env for testing. Empty => every high-risk command refused.
    public static string CommandSigningPublicKeyHex =>
        Environment.GetEnvironmentVariable("COMMAND_SIGNING_PUBLIC_KEY_HEX")
        ?? CommandSigningPublicKeyHexEmbedded;

    // TODO: paste the real 64-hex key from `python sign_release.py --genkey`
    // (the same value configured on the hub). Left blank => fail-closed.
    private const string CommandSigningPublicKeyHexEmbedded = "";

    // Ed25519 public key that verifies the signed self-update manifest. Reuses
    // the existing companion update key (UPDATE_PUBLIC_KEY_HEX in companion.py).
    public const string UpdatePublicKeyHex =
        "9a4f433e0eb82fae121fdeede7d2ce881d50bc80021236f24fdfa4494fc0537c";

    // Signed update manifest served from the repo's main branch, plus its
    // detached signature. The actual binary lives at the URL inside the manifest.
    // Overridable via env (mirrors HubBase) so the self-update path can be exercised
    // against a local server instead of raw.githubusercontent.com.
    public static string UpdateManifestUrl =>
        Environment.GetEnvironmentVariable("TEMP_MONITOR_UPDATE_MANIFEST_URL")
        ?? "https://raw.githubusercontent.com/aw08-2004/Temp_Monitor/main/agent/agent.manifest.json";
    public static string UpdateManifestSigUrl => UpdateManifestUrl + ".sig";

    // --- State paths (%ProgramData%\TempMonitorAgent) ----------------------
    public static string ProgramDataDir =>
        Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
            "TempMonitorAgent");

    public static string AgentIdentityPath => Path.Combine(ProgramDataDir, "agent.json");
    public static string RestartStatePath => Path.Combine(ProgramDataDir, "restart_state.json");
    public static string LogPath => Path.Combine(ProgramDataDir, "companion.log");
    public static string UpdateStagingDir => Path.Combine(ProgramDataDir, "update");

    // Registry location the installer writes the one-time enrollment secret to.
    public const string RegistryKeyPath = @"SOFTWARE\TempMonitorAgent";
    public const string RegistryEnrollmentSecretValue = "EnrollmentSecret";
}
