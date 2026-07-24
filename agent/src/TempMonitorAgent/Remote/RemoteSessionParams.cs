using System.Text.Json;
using System.Text.Json.Serialization;

namespace TempMonitorAgent.Remote;

/// <summary>
/// Parameters for one remote view/control session (roadmap #2), handed from the service to
/// the session-injected helper via a file (see AgentConfig.RemoteStateDir) rather than the
/// command line -- later phases add short-lived TURN credentials here, which must never sit
/// in the process list.
///
/// The shape is forward-compatible: the phase-1 helper only needs the session id and monitor,
/// but the signaling endpoint and ICE servers land in the same file in phase 3/4 without a
/// new delivery mechanism. System.Text.Json ignores unknown members, so an older helper reads
/// a newer file (and vice-versa) without throwing.
/// </summary>
public sealed class RemoteSessionParams
{
    /// <summary>Hub-minted session id. Correlates the helper's signaling with the console's
    /// viewer and the audit trail; also names diagnostics.</summary>
    [JsonPropertyName("session_id")] public string SessionId { get; set; } = "";

    /// <summary>Which monitor to capture (index into the active desktop's outputs). Default 0
    /// = primary. Multi-monitor switching is an open v1 decision (see the plan).</summary>
    [JsonPropertyName("monitor")] public int Monitor { get; set; }

    /// <summary>unattended (default) connects immediately; attended prompts the logged-in
    /// user first. Resolved by the hub from settings; enforced by the helper in phase 6.</summary>
    [JsonPropertyName("consent_mode")] public string ConsentMode { get; set; } = "unattended";

    /// <summary>The operator who started the session (from the hub's trusted session, never a
    /// client body). Carried for the attended banner and helper-side logging.</summary>
    [JsonPropertyName("issued_by")] public string IssuedBy { get; set; } = "";

    /// <summary>ICE servers (STUN/TURN) minted by the hub for this session. TURN entries carry
    /// short-lived credentials. Empty is valid -- on a LAN, host candidates alone connect.</summary>
    [JsonPropertyName("ice_servers")] public List<IceServerConfig> IceServers { get; set; } = new();

    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    public string ToJson() => JsonSerializer.Serialize(this, JsonOpts);

    public static RemoteSessionParams? FromJson(string json)
    {
        try { return JsonSerializer.Deserialize<RemoteSessionParams>(json, JsonOpts); }
        catch { return null; }
    }
}

/// <summary>One ICE server as minted by the hub (remote.ice_servers). `urls` is always a list
/// (the hub normalises it), and TURN entries additionally carry username + credential.</summary>
public sealed class IceServerConfig
{
    [JsonPropertyName("urls")] public List<string> Urls { get; set; } = new();
    [JsonPropertyName("username")] public string? Username { get; set; }
    [JsonPropertyName("credential")] public string? Credential { get; set; }
}
