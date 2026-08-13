using TempMonitorAgent;
using TempMonitorAgent.Telemetry;
using Xunit;

namespace TempMonitorAgent.Tests;

/// <summary>
/// The flag that decides how fast this machine reports, and the clamp around the interval
/// the hub asks for.
///
/// **What this protects is the machine's network, not the feature.** The hub sends the
/// cadence it wants, which is what lets a fleet on a metered link be dialled back without
/// updating every agent -- and is also a number that arrives over the wire and gets used as
/// a sleep. So the clamp is asserted rather than trusted: a zero, a negative or a missing
/// value must all leave this agent reporting at a cadence it chose.
///
/// **Default off, always.** A hub that says nothing (an older one, or a reply that did not
/// parse) has to mean the ordinary five-second cadence, because that is what such a hub
/// already expects and what an unwatched machine has always sent.
///
/// The names on the wire are asserted from the other side in tests/test_live_web.py.
/// </summary>
[Collection("live-telemetry")]   // static state -- these must not interleave
public class LiveTelemetryTests
{
    [Fact]
    public void NobodyWatchingMeansTheOrdinaryCadence()
    {
        LiveTelemetry.SetWanted(false);
        Assert.False(LiveTelemetry.Wanted);
        Assert.Equal(AgentConfig.IntervalSeconds, LiveTelemetry.IntervalSeconds);
    }

    [Fact]
    public void WatchedMeansTheFastCadence()
    {
        LiveTelemetry.SetWanted(true);
        try
        {
            Assert.True(LiveTelemetry.Wanted);
            Assert.Equal(AgentConfig.LiveIntervalSeconds, LiveTelemetry.IntervalSeconds);
        }
        finally { LiveTelemetry.SetWanted(false, AgentConfig.LiveIntervalSeconds); }
    }

    [Fact]
    public void TheHubMayAskForASlowerLiveCadence()
    {
        LiveTelemetry.SetWanted(true, 3);
        try { Assert.Equal(3, LiveTelemetry.IntervalSeconds); }
        finally { LiveTelemetry.SetWanted(false, AgentConfig.LiveIntervalSeconds); }
    }

    [Theory]
    [InlineData(0)]        // a hub that sent a 0 would be asking for a spin loop
    [InlineData(-5)]
    public void ButNeverForOneThisAgentIsNotWillingToRun(int asked)
    {
        LiveTelemetry.SetWanted(true, asked);
        try { Assert.True(LiveTelemetry.IntervalSeconds >= 1); }
        finally { LiveTelemetry.SetWanted(false, AgentConfig.LiveIntervalSeconds); }
    }

    [Fact]
    public void AndNeverForOneSlowerThanTheOrdinaryCadence()
    {
        // "Slower than normal while somebody is WATCHING" is not a thing to ask for; a hub
        // that wants that should clear the flag instead.
        LiveTelemetry.SetWanted(true, AgentConfig.IntervalSeconds * 10);
        try { Assert.Equal(AgentConfig.IntervalSeconds, LiveTelemetry.IntervalSeconds); }
        finally { LiveTelemetry.SetWanted(false, AgentConfig.LiveIntervalSeconds); }
    }

    [Fact]
    public void AHubThatSaysNothingAboutTheIntervalLeavesItAlone()
    {
        LiveTelemetry.SetWanted(true, 2);
        LiveTelemetry.SetWanted(true);            // flag only, no interval
        try { Assert.Equal(2, LiveTelemetry.IntervalSeconds); }
        finally { LiveTelemetry.SetWanted(false, AgentConfig.LiveIntervalSeconds); }
    }
}
