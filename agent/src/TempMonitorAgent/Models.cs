using System.Text.Json.Nodes;
using System.Text.Json.Serialization;

namespace TempMonitorAgent;

/// <summary>Persisted enrollment identity (agent.json). The token is returned by the
/// hub exactly once at enroll and cannot be recovered, so it must survive restarts.</summary>
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

/// <summary>Self-update loop guard (restart_state.json): how many times we've chained
/// a restart toward a given target version, so a bad update can't loop forever.</summary>
public sealed class RestartState
{
    [JsonPropertyName("target")] public string Target { get; set; } = "";
    [JsonPropertyName("count")] public int Count { get; set; }
}

/// <summary>A command as delivered by GET /api/agent/commands.
/// A pre-1.10 hub also sends requires_signature/signature; System.Text.Json ignores
/// unknown members by default, so this deserializes cleanly against either hub.</summary>
public sealed class FleetCommand
{
    [JsonPropertyName("id")] public string Id { get; set; } = "";
    [JsonPropertyName("type")] public string Type { get; set; } = "";
    [JsonPropertyName("params")] public JsonNode? Params { get; set; }

    /// <summary>The operator who issued this, set from the hub's trusted session (never a
    /// client body). The interactive terminal keys each operator's persistent shell on it,
    /// so one operator can't drive another's session. Absent on a pre-3.2 hub -> empty.</summary>
    [JsonPropertyName("issued_by")] public string IssuedBy { get; set; } = "";
}

/// <summary>Result of executing a command, reported back to the hub. <c>Cwd</c> is the
/// working directory a persistent shell was left in (run_script only); null otherwise, and
/// an older hub simply ignores the extra field.</summary>
public readonly record struct CommandResult(bool Success, string? Output, string? Cwd = null)
{
    public static CommandResult Ok(string? output = null) => new(true, output);
    public static CommandResult Fail(string output) => new(false, output);
}

/// <summary>One flattened leaf sensor, matching companion.py's flatten_sensors dict.</summary>
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

/// <summary>Hardware identity read once at startup (BIOS/chassis).</summary>
public sealed class SystemIdentity
{
    [JsonPropertyName("serial_number")] public string? SerialNumber { get; set; }
    [JsonPropertyName("model")] public string? Model { get; set; }
    [JsonPropertyName("asset_tag")] public string? AssetTag { get; set; }
    [JsonPropertyName("service_tag")] public string? ServiceTag { get; set; }
}

/// <summary>The signed self-update manifest (agent.manifest.json).</summary>
public sealed class UpdateManifest
{
    [JsonPropertyName("version")] public string Version { get; set; } = "";
    [JsonPropertyName("sha256")] public string Sha256 { get; set; } = "";
    [JsonPropertyName("url")] public string Url { get; set; } = "";
}
