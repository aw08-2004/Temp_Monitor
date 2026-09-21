using System.Collections.Generic;
using TempMonitorAgent;
using TempMonitorAgent.State;
using Xunit;

namespace TempMonitorAgent.Tests;

/// <summary>
/// The two hub-settable telemetry cadences (roadmap #3), and the clamp around them.
///
/// **The silent failure this exists to catch is a fleet that stops reporting.** These two
/// numbers become a <c>Task.Delay</c> on the loop that sends every reading, and they arrive
/// over the wire as text. A 0 is a spin loop, a negative is an immediate throw, and a value
/// parsed under a comma-decimal locale is a machine that quietly reports at the compiled
/// cadence while the console says otherwise -- none of which announces itself, because a
/// machine still heartbeats and still reads online through all of it.
///
/// **And a hub that has nothing to say must change nothing.** A payload without these keys is
/// the normal case on every hub older than the setting, so the compiled cadence has to
/// survive it -- as does an unparseable value, per Apply's rule that a malformed push never
/// blanks out working settings.
/// </summary>
public class CadenceTests
{
    private static RuntimeConfig Apply(params (string Key, string Value)[] pairs)
    {
        var payload = new Dictionary<string, object?>();
        foreach (var (key, value) in pairs) payload[key] = value;
        return RuntimeConfig.Default.Apply(payload, "v1");
    }

    [Fact]
    public void TheCompiledCadenceIsWhatTheHubDefaultsTo()
    {
        // The registry's defaults MUST equal these, or an untouched hub would change the
        // cadence of every machine in the field the first time it pushed a config.
        Assert.Equal(AgentConfig.IntervalSeconds, RuntimeConfig.Default.ReportIntervalSeconds);
        Assert.Equal(AgentConfig.SensorIntervalSeconds,
                     RuntimeConfig.Default.SensorIntervalSeconds);
    }

    [Fact]
    public void TheHubCanSlowReportingDown()
    {
        var applied = Apply(("metrics.report_interval_seconds", "60"),
                            ("metrics.sensor_interval_seconds", "120"));

        Assert.Equal(60, applied.ReportIntervalSeconds);
        Assert.Equal(120, applied.EffectiveSensorIntervalSeconds);
    }

    [Fact]
    public void ASensorCadenceFasterThanTheReportCadenceMeansEveryReport()
    {
        // Not an error: a sensor block can only ride on a report, so "1" is the natural way
        // to ask for the block on every one of them.
        var applied = Apply(("metrics.report_interval_seconds", "30"),
                            ("metrics.sensor_interval_seconds", "1"));

        Assert.Equal(30, applied.EffectiveSensorIntervalSeconds);
    }

    [Theory]
    [InlineData("0")]
    [InlineData("-5")]
    [InlineData("100000")]
    public void ButNeverOneThisAgentIsNotWillingToRun(string asked)
    {
        var applied = Apply(("metrics.report_interval_seconds", asked),
                            ("metrics.sensor_interval_seconds", asked));

        Assert.InRange(applied.ReportIntervalSeconds,
                       RuntimeConfig.MinIntervalSeconds,
                       RuntimeConfig.MaxReportIntervalSeconds);
        Assert.InRange(applied.SensorIntervalSeconds,
                       RuntimeConfig.MinIntervalSeconds,
                       RuntimeConfig.MaxSensorIntervalSeconds);
    }

    [Fact]
    public void AHubThatSaysNothingLeavesTheCadenceAlone()
    {
        // Every hub older than this setting, on every heartbeat.
        var applied = Apply(("metrics.collect_network", "false"));

        Assert.Equal(AgentConfig.IntervalSeconds, applied.ReportIntervalSeconds);
        Assert.Equal(AgentConfig.SensorIntervalSeconds, applied.SensorIntervalSeconds);
    }

    [Theory]
    [InlineData("")]
    [InlineData("soon")]
    [InlineData("10,5")]      // a comma-decimal locale leaking into the payload
    [InlineData("10.5")]
    public void AndSoDoesAValueThatIsNotAWholeNumberOfSeconds(string asked)
    {
        var slowed = RuntimeConfig.Default with { ReportIntervalSeconds = 45 };
        var applied = slowed.Apply(
            new Dictionary<string, object?> { ["metrics.report_interval_seconds"] = asked },
            "v2");

        // 45, not the compiled 5: a malformed push must not reset a cadence that was set.
        Assert.Equal(45, applied.ReportIntervalSeconds);
    }

    [Fact]
    public void ApplyingACadenceNeverDisturbsTheChannel()
    {
        // Same guarantee ChannelTests asserts for the sensor preference: the channel travels
        // its own per-machine heartbeat field, so nothing in a config block may touch it.
        var onBeta = RuntimeConfig.Default with { Channel = Channels.Beta };
        var applied = onBeta.Apply(
            new Dictionary<string, object?> { ["metrics.report_interval_seconds"] = "30" },
            "v3");

        Assert.Equal(Channels.Beta, applied.Channel);
        Assert.Equal(30, applied.ReportIntervalSeconds);
    }
}
