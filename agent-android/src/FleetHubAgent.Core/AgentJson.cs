using System.Text.Json.Serialization;
using FleetHubAgent.Fleet;

namespace FleetHubAgent;

/// <summary>
/// The source-generated serialization metadata for every typed JSON read and write this agent
/// makes. **Every JsonSerializer call in this project passes a JsonTypeInfo from here, and one
/// that does not is a bug that only exists on a phone.**
///
/// The Release APK is trimmed fully (see the Android csproj), and a trimmed .NET app ships with
/// `System.Text.Json.JsonSerializer.IsReflectionEnabledByDefault=false`. A reflection-based call
/// -- `JsonSerializer.Serialize(identity)`, with no type info -- does not degrade on a device: it
/// throws InvalidOperationException, and on a workstation it works. So 0.2.1 enrolled against
/// the hub, failed to serialize the token it was handed, discarded it by design (a token that is
/// not on disk must not be adopted), and did that again every thirty seconds, forever. The same
/// throw stopped every telemetry report, every command poll and the self-update manifest read.
/// **Observed on a factory-reset phone provisioned by QR**, and by setup screen, identically.
///
/// **Rejected: turning reflection back on** (`JsonSerializerIsReflectionEnabledByDefault=true`).
/// It compiles, and it is worse than the throw. The linker still removes the property metadata
/// reflection would read, so a trimmed build then serializes an identity as `{}` -- no exception,
/// a stored token that loads as blank, and the same loop with nothing in the log at all.
///
/// **Rejected: dropping AndroidLinkMode back to the default.** It fixes this and puts five
/// megabytes of BouncyCastle back into an APK every managed phone downloads on every release;
/// the csproj argues that trade. Source generation costs one file.
///
/// **What keeps this honest is the test project, not this comment.** It runs with reflection
/// switched off (its csproj), so a new reflection-based call fails `dotnet test` on a workstation
/// instead of failing on a device that has just been factory reset. See JsonTrimmingTests.
/// </summary>
[JsonSerializable(typeof(AgentIdentity))]
[JsonSerializable(typeof(RestartState))]
[JsonSerializable(typeof(UpdateManifest))]
[JsonSerializable(typeof(FleetClient.CommandsResponse))]
// The telemetry payload is a dictionary of `object`, and a source-generated `object` is written
// by looking up its RUNTIME type in this context. So each type a payload value can actually hold
// is listed -- one missing here throws exactly the way the whole payload used to, but only on the
// report that carries it. TelemetryReporter.BuildPayload normalises sensors to a List for this
// reason.
[JsonSerializable(typeof(Dictionary<string, object>))]
[JsonSerializable(typeof(List<SensorReading>))]
[JsonSerializable(typeof(string))]
[JsonSerializable(typeof(double))]
[JsonSerializable(typeof(long))]
internal sealed partial class AgentJson : JsonSerializerContext
{
}
