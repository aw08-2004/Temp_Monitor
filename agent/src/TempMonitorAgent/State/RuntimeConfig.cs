using System.Text.Json.Serialization;

namespace TempMonitorAgent.State;

/// <summary>
/// Hub-delivered operational configuration, plus the compiled-in defaults it overrides.
///
/// Delivered over the authenticated heartbeat (see FleetClient.HeartbeatAsync) and
/// persisted to %ProgramData%\TempMonitorAgent\config.json so a service restart or a
/// self-update doesn't run on compiled defaults until the next heartbeat lands —
/// otherwise every reboot would read the "wrong" sensor for the first ~10 seconds.
///
/// ONLY OPERATIONAL TUNING TRAVELS THIS CHANNEL. Nothing here may redirect where the
/// agent gets its code or which key verifies it: not the update manifest URL, not the
/// Ed25519 update key, not the hub base URL, not the registry path. Per fleet.py's
/// module docstring the signed-manifest chain is the one control that still holds if the
/// hub itself is compromised, and a hub-settable trust root would trade that away.
/// The enforcement is structural rather than a rule to remember: Apply() copies named
/// fields off an allow-list, so an unknown or trust-bearing key in the payload is simply
/// never read. Extend the allow-list, never replace it with a deny-list.
/// </summary>
public sealed record RuntimeConfig
{
    /// <summary>Best-first CPU temperature preference, matched as a lowercased substring
    /// of the sensor name. Default mirrors companion.py's PREFERRED_SENSORS.</summary>
    public IReadOnlyList<string> PreferredSensors { get; init; } = new[]
    {
        "cpu package",
        "core (tctl/tdie)",
        "core average",
        "core max",
        "cpu cores",
    };

    /// <summary>Exact sensor name pinned for this machine, or null to use the list above.
    /// Matched exactly (case-insensitively), unlike the fuzzy preference list.</summary>
    public string? PrimarySensorName { get; init; }

    /// <summary>Whether to collect and report the network sensor category (throughput in/out).
    /// Mirrors the hub's metrics.collect_network toggle; off means the NIC category is not
    /// reported at all. Default true.</summary>
    public bool CollectNetwork { get; init; } = true;

    /// <summary>Content hash of the config the hub last sent. Echoed back on each
    /// heartbeat so the hub can skip re-sending an unchanged payload. Empty means
    /// "never received any", which is what makes the first heartbeat fetch it.</summary>
    public string ConfigVersion { get; init; } = "";

    public static RuntimeConfig Default { get; } = new();

    /// <summary>
    /// Build a new config from a hub payload, keyed by the hub's registry keys.
    /// Unrecognised keys are ignored — see the allow-list note in the type docs.
    /// Returns the current instance unchanged if the payload carries nothing usable,
    /// so a malformed push can never blank out working settings.
    /// </summary>
    public RuntimeConfig Apply(IReadOnlyDictionary<string, object?>? payload, string version)
    {
        if (payload is null) return this with { ConfigVersion = version };

        var preferred = PreferredSensors;
        if (payload.TryGetValue("computer.primary_sensor_preference", out var raw)
            && raw is IEnumerable<object?> items)
        {
            var parsed = items
                .Select(v => v?.ToString()?.Trim().ToLowerInvariant())
                .Where(v => !string.IsNullOrEmpty(v))
                .Select(v => v!)
                .ToArray();
            // An empty list would silently disable sensor selection entirely; treat it
            // as "nothing to say" and keep what we had.
            if (parsed.Length > 0) preferred = parsed;
        }

        // Booleans arrive as the strings "true"/"false" (see FleetClient's payload build,
        // which ToString()s every non-array value). Anything else leaves the flag untouched.
        var collectNetwork = CollectNetwork;
        if (payload.TryGetValue("metrics.collect_network", out var netRaw) && netRaw is not null)
        {
            var text = netRaw.ToString()?.Trim().ToLowerInvariant();
            if (text is "true" or "false") collectNetwork = text == "true";
        }

        return this with
        {
            PreferredSensors = preferred,
            CollectNetwork = collectNetwork,
            ConfigVersion = version,
        };
    }
}

/// <summary>
/// Process-wide holder for the active <see cref="RuntimeConfig"/>.
///
/// Copy-on-write, mirroring settings.py's cache on the hub: readers take the reference
/// once and only read the immutable record it points at, writers swap the whole
/// reference. Reads land on the sensor loop every few seconds and writes only when the
/// hub pushes a change, so this keeps the read path free of locking while making a torn
/// read impossible. Volatile because the write and the reads are not guaranteed to be on
/// the same thread.
/// </summary>
public static class RuntimeConfigStore
{
    private static volatile RuntimeConfig _current = RuntimeConfig.Default;

    public static RuntimeConfig Current => _current;

    public static void Set(RuntimeConfig config) => _current = config;
}
