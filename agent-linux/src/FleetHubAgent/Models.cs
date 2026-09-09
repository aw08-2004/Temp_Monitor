using System.Text.Json.Nodes;
using System.Text.Json.Serialization;

namespace FleetHubAgent;

// The wire shapes, deliberately identical to the Windows agent's Models.cs.
//
// They are duplicated rather than shared, and that is a decision worth stating: extracting a
// common project would mean editing the csproj and moving files of an agent that is installed
// on every managed Windows PC and self-updates from a signed manifest. The cost of getting
// that wrong is a fleet that stops updating, which is exactly the failure CLAUDE.md's
// versioning rules exist to prevent. So the Linux agent starts standalone and earns the right
// to a shared core once it has a release train of its own.
//
// What keeps the two copies honest is the hub: these names are the hub's field names, and a
// drift here shows up as a machine reporting nothing rather than as a silent disagreement.

/// <summary>What a self-update is aiming at, and how many times it has tried.
///
/// **The count is the whole point.** systemd restarts this unit on any exit, so a build that
/// stages, starts and immediately dies would otherwise be downloaded and run forever, ten
/// seconds apart, on every machine that took the release. Persisted rather than held in memory
/// because the process that would remember it is the one that keeps dying.</summary>
public sealed class RestartState
{
    [JsonPropertyName("target")] public string Target { get; set; } = "";
    [JsonPropertyName("count")] public int Count { get; set; }
}

/// <summary>The signed self-update manifest: which version, where, and what it hashes to.
///
/// Field names are the ones sign_release.py writes and the Windows agent already reads. The
/// sha256 is the load-bearing field: it is covered by the signature, so it is what makes the
/// download itself untrusted -- the binary can come from anywhere as long as it hashes to what
/// the signed manifest said.</summary>
public sealed class UpdateManifest
{
    [JsonPropertyName("version")] public string Version { get; set; } = "";
    [JsonPropertyName("sha256")] public string Sha256 { get; set; } = "";
    [JsonPropertyName("url")] public string Url { get; set; } = "";
}

/// <summary>Persisted enrollment identity (agent.json). The token is returned by the hub
/// exactly once at enroll and cannot be recovered, so it must survive restarts.</summary>
public sealed class AgentIdentity
{
    [JsonPropertyName("agent_id")] public string AgentId { get; set; } = "";
    [JsonPropertyName("token")] public string Token { get; set; } = "";

    [JsonIgnore]
    public bool IsEnrolled => !string.IsNullOrEmpty(AgentId) && !string.IsNullOrEmpty(Token);

    /// <summary>Value for the Authorization header: "Bearer &lt;agent_id&gt;:&lt;token&gt;".</summary>
    [JsonIgnore]
    public string BearerValue => $"{AgentId}:{Token}";
}

/// <summary>A command as delivered by GET /api/agent/commands.</summary>
public sealed class FleetCommand
{
    [JsonPropertyName("id")] public string Id { get; set; } = "";
    [JsonPropertyName("type")] public string Type { get; set; } = "";
    [JsonPropertyName("params")] public JsonNode? Params { get; set; }

    /// <summary>The operator who issued this, set from the hub's trusted session (never a
    /// client body).</summary>
    [JsonPropertyName("issued_by")] public string IssuedBy { get; set; } = "";
}

/// <summary>One answer from the command endpoint: what to run, and whether the hub HELD the
/// request open waiting for it.
///
/// <c>Waited</c> is the whole reason this is a pair rather than a list. When the hub holds a
/// request, the wait has already happened inside it, so the poll loop must come straight back
/// round -- sleeping afterwards would reintroduce exactly the delay the hold removed. When it
/// declines (push disabled, its cap full, or an older hub), the loop keeps its own cadence.
/// An empty list therefore means two very different things, and this is how they are told
/// apart.</summary>
public readonly record struct CommandPollResult(List<FleetCommand> Commands, bool Waited)
{
    public static CommandPollResult Empty => new(new List<FleetCommand>(), false);
}

/// <summary>Result of executing a command, reported back to the hub.</summary>
public readonly record struct CommandResult(bool Success, string? Output)
{
    public static CommandResult Ok(string? output = null) => new(true, output);
    public static CommandResult Fail(string output) => new(false, output);
}

/// <summary>One flattened leaf sensor, matching the hub's flattened sensor shape.
///
/// **The hub reads this by Type and by what HardwareId contains**, never by position -- see
/// app.py's _find_sensor_value, which builds a haystack out of hardware_id plus hardware and
/// looks for "cpu"/"gpu"/"ram" in it. That is the contract the Linux readers have to satisfy
/// to make a machine's overview page populate, and it is why ProcSensorReader emits synthetic
/// identifiers like "/cpu/0" rather than the hwmon chip names it actually read.</summary>
public sealed class SensorReading
{
    [JsonPropertyName("hardware")] public string? Hardware { get; set; }
    [JsonPropertyName("hardware_id")] public string? HardwareId { get; set; }
    [JsonPropertyName("group")] public string? Group { get; set; }
    [JsonPropertyName("name")] public string? Name { get; set; }
    [JsonPropertyName("type")] public string? Type { get; set; }
    [JsonPropertyName("value")] public double? Value { get; set; }
    [JsonPropertyName("text")] public string? Text { get; set; }
}

/// <summary>Hardware identity read once at startup (DMI), plus the operating system on top
/// of it.
///
/// Read once, like the Windows agent's, and correct for the same reason: a machine that is
/// re-imaged or dist-upgraded across a major release reboots to get there, and this process
/// starts again with it.
///
/// The field names are the hub's Windows-shaped ones (os_caption, os_build) because the hub
/// stores and buckets them by those names. os_build carries the KERNEL release here, which is
/// the nearest true equivalent -- and is safe to send because normalize_os only parses a
/// build number when the caption is Windows-shaped or unrecognised.</summary>
public sealed class SystemIdentity
{
    [JsonPropertyName("serial_number")] public string? SerialNumber { get; set; }
    [JsonPropertyName("model")] public string? Model { get; set; }
    [JsonPropertyName("manufacturer")] public string? Manufacturer { get; set; }
    [JsonPropertyName("asset_tag")] public string? AssetTag { get; set; }
    [JsonPropertyName("os_caption")] public string? OsCaption { get; set; }
    [JsonPropertyName("os_version")] public string? OsVersion { get; set; }
    [JsonPropertyName("os_build")] public string? OsBuild { get; set; }
    [JsonPropertyName("os_arch")] public string? OsArchitecture { get; set; }
}
