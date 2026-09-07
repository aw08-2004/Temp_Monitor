using System.Text.Json.Serialization;
using System.Text.Json.Nodes;

namespace FleetHubAgent;

// The wire shapes, deliberately identical to the Windows and Linux agents' Models.cs.
//
// They are duplicated rather than shared, and the argument is agent-linux's unchanged:
// extracting a project common to all of them would mean editing the csproj and moving files of
// an agent that is installed on every managed Windows PC and self-updates from a signed
// manifest. The cost of getting that wrong is a fleet that stops updating, which is exactly
// the failure CLAUDE.md's versioning rules exist to prevent.
//
// What keeps the copies honest is the hub: these names are the hub's field names, and a drift
// here shows up as a machine reporting nothing rather than as a silent disagreement.

/// <summary>Persisted enrollment identity. The token is returned by the hub exactly once at
/// enroll and cannot be recovered, so it must survive restarts -- and on Android it must also
/// survive the process being killed at any moment, which is the whole reason IStateStore
/// commits synchronously.</summary>
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
/// looks for "cpu"/"gpu"/"ram" in it. That is the contract the Android readers have to satisfy
/// to make a device's overview page populate, and it is why BatterySensorReader emits
/// synthetic identifiers like "/cpu/0" and "/ram" rather than the thermal zone names it
/// actually read.</summary>
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

/// <summary>Hardware identity read once at startup, plus the operating system on top of it.
///
/// Read once, like every other agent's, and correct for the same reason: a device that is
/// factory reset or takes a major OS upgrade reboots to get there, and this process starts
/// again with it.
///
/// The field names are the hub's Windows-shaped ones because the hub stores and buckets them
/// by those names. What each one carries on Android is documented on AndroidSystemInfo, and
/// two of them are NOT what their name suggests -- serial_number is not a hardware serial and
/// os_build is not a Windows build number. Read that file before trusting either.</summary>
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
