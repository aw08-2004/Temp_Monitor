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
    /// of the sensor name. Mirrored by the hub's settings.DEFAULT_SENSOR_PREFERENCE.</summary>
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

    /// <summary>
    /// How often to report (seconds), mirroring the hub's metrics.report_interval_seconds.
    /// Defaults to this build's compiled cadence, which is what a machine keeps running at
    /// when the hub is older than the setting (roadmap #3).
    ///
    /// <para>This is the ORDINARY cadence only. While an operator has this machine's page
    /// open the loop runs at <see cref="LiveTelemetry"/>'s interval instead, because that
    /// cadence exists to make a three-second spike visible and a fleet-wide default has no
    /// business overriding somebody who is actually watching.</para>
    ///
    /// <para>It is not liveness either: the heartbeat is its own loop on its own
    /// <see cref="AgentConfig.HeartbeatSeconds"/>, and that is what decides whether this
    /// machine reads online. Slowing reporting right down costs chart resolution and
    /// nothing else.</para>
    /// </summary>
    public int ReportIntervalSeconds { get; init; } = AgentConfig.IntervalSeconds;

    /// <summary>How often a report carries the FULL sensor block (seconds), mirroring the
    /// hub's metrics.sensor_interval_seconds. See
    /// <see cref="EffectiveSensorIntervalSeconds"/> for why it is not used raw.</summary>
    public int SensorIntervalSeconds { get; init; } = AgentConfig.SensorIntervalSeconds;

    /// <summary>The sensor cadence actually applied: never faster than the report cadence,
    /// because a sensor block can only ride on a report.
    ///
    /// <para>Clamped rather than refused. "Put the full block on every report" is a sensible
    /// thing for an operator to want and the natural way to ask for it is a small number, so
    /// 1 against a 5-second report cadence means every report -- not a broken setting.</para>
    /// </summary>
    [JsonIgnore]
    public int EffectiveSensorIntervalSeconds =>
        Math.Max(SensorIntervalSeconds, ReportIntervalSeconds);

    /// <summary>Bounds on the two hub-supplied cadences, mirroring the registry's own
    /// minimum/maximum in settings.py.
    ///
    /// <para>Enforced here as well as there on purpose, the same way
    /// <see cref="LiveTelemetry"/> clamps the live interval: the hub validates what an
    /// operator types, but this process decides what it is willing to do to its own CPU and
    /// its own network, and a value that arrives out of range (a hub bug, a hand-edited
    /// config.json) must not be able to put a fleet into a sensor read every 50 ms.</para>
    /// </summary>
    public const int MinIntervalSeconds = 1;
    public const int MaxReportIntervalSeconds = 300;
    public const int MaxSensorIntervalSeconds = 3600;

    /// <summary>Content hash of the config the hub last sent. Echoed back on each
    /// heartbeat so the hub can skip re-sending an unchanged payload. Empty means
    /// "never received any", which is what makes the first heartbeat fetch it.</summary>
    public string ConfigVersion { get; init; } = "";

    /// <summary>
    /// Which release channel this machine follows: <c>"stable"</c> or <c>"beta"</c>
    /// (roadmap #21). A NAME, never a URL — and that distinction is the entire reason this
    /// field is allowed to exist at all.
    ///
    /// <para>The type docs above forbid the hub from redirecting where the agent gets its
    /// code. A channel name does not: <see cref="AgentConfig.UpdateManifestUrl"/> maps it
    /// onto one of two COMPILED-IN urls, so the worst a compromised hub can do is move this
    /// machine onto the other train — where the manifest is still signed by the same offline
    /// release key, and the binary's sha256 is still checked against that signed value. It
    /// cannot introduce a destination the agent did not already trust. Storing a url here
    /// instead would trade that away, which is why <see cref="Apply"/> reads only this name
    /// and why anything unrecognised falls back to stable.</para>
    ///
    /// <para>Persisted with the rest of the config so a service restart does not silently
    /// drop a pilot machine back to stable for one heartbeat and then move it again.</para>
    /// </summary>
    public string Channel { get; init; } = Channels.Stable;

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
            ReportIntervalSeconds = Seconds(
                payload, "metrics.report_interval_seconds", ReportIntervalSeconds,
                MaxReportIntervalSeconds),
            SensorIntervalSeconds = Seconds(
                payload, "metrics.sensor_interval_seconds", SensorIntervalSeconds,
                MaxSensorIntervalSeconds),
            ConfigVersion = version,
        };
    }

    /// <summary>One cadence off the payload, clamped, or <paramref name="fallback"/> when the
    /// key is absent or unparseable.
    ///
    /// <para>Numbers arrive as STRINGS, like the booleans above and for the same reason: the
    /// heartbeat payload ToString()s every non-array value. Parsed with the invariant culture
    /// -- a machine on a German or Spanish locale must read "10" as ten either way, and this
    /// is the class of bug that only shows up on the one PC in the fleet that has it.</para>
    ///
    /// <para>An unparseable value leaves the cadence alone rather than resetting it to the
    /// compiled default, matching Apply's rule that a malformed push can never blank out
    /// working settings.</para>
    /// </summary>
    private static int Seconds(IReadOnlyDictionary<string, object?> payload, string key,
                              int fallback, int max)
    {
        if (!payload.TryGetValue(key, out var raw) || raw is null) return fallback;
        var text = raw.ToString()?.Trim();
        if (!int.TryParse(text, System.Globalization.NumberStyles.Integer,
                          System.Globalization.CultureInfo.InvariantCulture, out var value))
            return fallback;
        return Math.Clamp(value, MinIntervalSeconds, max);
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
