using System.Text.Json.Serialization;

namespace FleetHubAgent.Telemetry;

[JsonSerializable(typeof(Dictionary<string, object>))]
internal partial class TelemetryJsonContext : JsonSerializerContext
{
}
