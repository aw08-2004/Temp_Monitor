namespace FleetHubAgent.Tests;

/// <summary>
/// The origin check that decides whether a request carries this agent's bearer token.
///
/// Ported from the Windows agent along with the function itself, because the attack it stops
/// is not platform-specific and the naive version looks correct: a StartsWith(HubBase) test
/// says yes to https://your.hub.url.attacker.net/x and to
/// https://your.hub.url@attacker.net/x, whose real hosts are attacker.net in both cases.
/// Either one hands the agent's credential to whoever supplied the URL -- and payload URLs are
/// operator-supplied by design.
///
/// Nothing calls IsHubUrl yet; the executors that would are not ported. It is tested anyway so
/// that the first thing to need it inherits a checked function rather than re-deriving one.
/// </summary>
public class HubUrlTests
{
    /// <summary>The compiled-in default, which is what these run against. Restated rather than
    /// read from AgentConfig so a change to the default fails this test loudly instead of
    /// quietly re-pointing every case at somewhere new.</summary>
    private const string Hub = "https://your.hub.url";

    [Fact]
    public void The_hub_itself_matches() => Assert.True(AgentConfig.IsHubUrl(Hub + "/api/report"));

    [Fact]
    public void Host_case_is_ignored() =>
        Assert.True(AgentConfig.IsHubUrl("https://your.hub.url/api/report"));

    [Fact]
    public void The_default_port_is_filled_in() =>
        Assert.True(AgentConfig.IsHubUrl("https://your.hub.url:443/x"));

    [Theory]
    // The two that a StartsWith check gets wrong, which is the reason this function exists.
    [InlineData("https://your.hub.url.attacker.net/x")]
    [InlineData("https://your.hub.url@attacker.net/x")]
    // ...and the ordinary mismatches.
    [InlineData("http://your.hub.url/x")]        // scheme
    [InlineData("https://your.hub.url:8443/x")]  // port
    [InlineData("https://attacker.net/x")]
    [InlineData("/api/report")]                       // relative: no origin to compare
    [InlineData("")]
    [InlineData(null)]
    public void Everything_else_is_refused(string? url) => Assert.False(AgentConfig.IsHubUrl(url));
}
