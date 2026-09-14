using TempMonitorAgent.Fleet;

namespace TempMonitorAgent.Tests;

/// <summary>
/// Covers the one decision that stands between a decommissioned PC and a machine that is
/// telemetry-only forever: what the agent does with a 401 from the hub.
///
/// The enrollment identity outlives the process, the install and a self-update -- it is a
/// file in %ProgramData% -- so an agent that keeps a credential the hub has stopped
/// recognising keeps it for good. An operator who deletes a machine and reinstalls the agent
/// with the correct secret sees no change and no error, because the agent never asks the hub
/// again. That is what ShouldReenrollAfter401 exists to end, and why it must default to
/// "re-enroll" rather than "stay down": the only thing that may pin an agent down is the hub
/// explicitly saying `revoked`, because a silent stay-down is indistinguishable from the bug.
/// </summary>
public class ReenrollmentTests
{
    [Theory]
    // The hub deleted the row -- the case an operator hits on every decommission.
    [InlineData("{\"error\":\"agent authentication required\",\"reason\":\"unknown\"}", true)]
    // No reason at all: a hub older than this change. Deploy order is hub first, then agent,
    // but a self-updated agent can still lead its hub by a few minutes.
    [InlineData("{\"error\":\"agent authentication required\"}", true)]
    [InlineData("", true)]
    [InlineData("   ", true)]
    // Not JSON at all -- a proxy error page, say. Still not a revocation.
    [InlineData("<html>502 Bad Gateway</html>", true)]
    [InlineData("{\"reason\":null}", true)]
    // ...and the one answer that must stop us, in whatever case the hub sends it.
    [InlineData("{\"error\":\"agent authentication required\",\"reason\":\"revoked\"}", false)]
    [InlineData("{\"reason\":\"REVOKED\"}", false)]
    public void DecidesFromTheHubsReason(string body, bool expected)
        => Assert.Equal(expected, FleetClient.ShouldReenrollAfter401(body));

    /// <summary>A revocation buried in some other field is not a revocation. The check reads
    /// one named key rather than sniffing the body, so an error *message* that happens to
    /// contain the word cannot pin an agent down.</summary>
    [Fact]
    public void OnlyTheReasonFieldCounts()
        => Assert.True(FleetClient.ShouldReenrollAfter401("{\"error\":\"revoked\"}"));
}
