using System.Text.Json.Serialization;

namespace FleetHubAgent.Telemetry;

/// <summary>
/// Source-generated serializer for the /api/report body, because the Android build links in
/// full mode and reflection-based System.Text.Json is not something to rely on after trimming.
///
/// **Every runtime type that can sit in the payload's object values must be listed here.**
/// The dictionary is declared as object, so the generator cannot see what goes in it; an
/// unlisted type compiles cleanly and throws NotSupportedException on the first report instead.
/// Today that is string (identity, machine, version), double (temp), long (client_ts, and
/// uptime_seconds boxed from long?) and List&lt;SensorReading&gt; (what AndroidSensorReader
/// builds). Add to this list whenever BuildPayload gains a field of a new type.
/// </summary>
[JsonSerializable(typeof(Dictionary<string, object?>))]
[JsonSerializable(typeof(string))]
[JsonSerializable(typeof(double))]
[JsonSerializable(typeof(long))]
[JsonSerializable(typeof(List<SensorReading>))]
internal partial class TelemetryJsonContext : JsonSerializerContext
{
}
