using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging.Abstractions;
using FleetHubAgent.Fleet;
using FleetHubAgent.State;
using FleetHubAgent.Telemetry;

namespace FleetHubAgent.Core.Tests;

/// <summary>
/// Everything this agent turns into or out of JSON with a type, run with reflection-based
/// System.Text.Json switched off -- the way it is on a phone.
///
/// **The silent failure this file exists to catch is a device that enrolls and cannot keep the
/// token.** The Release APK is trimmed fully, and a trimmed app ships with reflection-based
/// serialization disabled. `JsonSerializer.Serialize(identity)` then throws rather than
/// serializes, AgentState.SaveIdentity catches it and reports a failed write, and FleetClient --
/// correctly, by its own rules -- discards an identity it could not persist and tries again in
/// thirty seconds. Forever. The hub mints a fresh agent row on every attempt, the device never
/// holds one, and the setup screen says "not enrolled yet, retrying" whether the secret came
/// from a QR or was typed in by hand. **Observed on a factory-reset phone provisioned by QR
/// with 0.2.1**; every test in the suite passed, because a workstation test run has reflection
/// on by default.
///
/// The test project turns it off (see its csproj), so the first assertion here checks that it
/// still is. Without it, the rest of this file would pass for the wrong reason.
/// </summary>
public class JsonTrimmingTests
{
    [Fact]
    public void ReflectionSerializationIsOffInThisTestRun()
    {
        // If this fails, somebody removed JsonSerializerIsReflectionEnabledByDefault from the
        // test csproj, and every other test in this file has stopped meaning anything.
        Assert.False(JsonSerializer.IsReflectionEnabledByDefault);
    }

    [Fact]
    public void AnEnrollmentIdentityCanBeStoredAndReadBack()
    {
        var store = new FakeStateStore();
        var identity = new AgentIdentity { AgentId = "a1b2", Token = "secret-token" };

        Assert.True(new AgentState(store).SaveIdentity(identity));

        // A fresh AgentState over the same store: what a restarted process sees.
        var loaded = new AgentState(store).LoadIdentity();
        Assert.True(loaded.IsEnrolled);
        Assert.Equal("a1b2", loaded.AgentId);
        Assert.Equal("secret-token", loaded.Token);
    }

    [Fact]
    public void AnIdentityStoredByAnEarlierBuildStillLoads()
    {
        // The exact shape every build so far has written. A device that enrolled on an untrimmed
        // build must not lose its identity to a change of serializer -- that is a second machine
        // with the same name in the console.
        var store = new FakeStateStore();
        store.Set(StateKeys.Identity, "{\"agent_id\":\"old-id\",\"token\":\"old-token\"}");

        var loaded = new AgentState(store).LoadIdentity();
        Assert.Equal("old-id", loaded.AgentId);
        Assert.Equal("old-token", loaded.Token);
    }

    [Fact]
    public void TheUpdateAttemptCounterCanBeStoredAndReadBack()
    {
        // The restart-loop guard. Lost, it installs a build that dies on startup on every tick.
        var store = new FakeStateStore();
        var state = new AgentState(store);

        Assert.True(state.SaveRestartState(new RestartState { Target = "0.2.2", Count = 2 }));

        var loaded = new AgentState(store).LoadRestartState();
        Assert.NotNull(loaded);
        Assert.Equal("0.2.2", loaded!.Target);
        Assert.Equal(2, loaded.Count);
    }

    [Fact]
    public void ATelemetryReportSerializesWithEverythingInIt()
    {
        // The report is what makes a device appear in the console at all, enrolled or not. Its
        // values are `object`, which a generated serializer writes by runtime type -- so the
        // sensors go in as an ARRAY here, deliberately not the List the real reader produces.
        using var reporter = new TelemetryReporter(
            NullLogger<TelemetryReporter>.Instance,
            new SystemIdentity { SerialNumber = "ssaid1234", Model = "Galaxy A15" },
            new MachineNameProvider(new AgentState(new FakeStateStore()), "Galaxy-A15-abcd1234"));
        var sensors = new[]
        {
            new SensorReading { HardwareId = "/cpu/0", Type = "Temperature", Value = 36.5 },
        };

        var json = JsonNode.Parse(
            TelemetryReporter.Serialize(reporter.BuildPayload(36.5, sensors, 1200)))!.AsObject();

        Assert.Equal("Galaxy-A15-abcd1234", json["machine"]!.GetValue<string>());
        Assert.Equal(36.5, json["temp"]!.GetValue<double>());
        Assert.Equal(1200, json["uptime_seconds"]!.GetValue<long>());
        Assert.Equal("ssaid1234", json["serial_number"]!.GetValue<string>());
        Assert.Equal("/cpu/0", json["sensors"]![0]!["hardware_id"]!.GetValue<string>());
        // A null the hub COALESCEs away must still be sent as a key, not crash the writer.
        Assert.True(json.ContainsKey("asset_tag"));
    }

    [Fact]
    public void ACommandPollBodyParsesWithItsParameters()
    {
        var result = FleetClient.ParseCommands(
            "{\"commands\":[{\"id\":\"c1\",\"type\":\"rename\",\"params\":{\"name\":\"Front-desk\"}," +
            "\"issued_by\":\"op@example.com\"}],\"waited\":true}");

        Assert.True(result.Waited);
        var command = Assert.Single(result.Commands);
        Assert.Equal("c1", command.Id);
        Assert.Equal("rename", command.Type);
        Assert.Equal("op@example.com", command.IssuedBy);
        Assert.Equal("Front-desk", command.Params!["name"]!.GetValue<string>());
    }
}
