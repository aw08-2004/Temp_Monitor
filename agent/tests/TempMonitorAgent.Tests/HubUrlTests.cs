using Xunit;

namespace TempMonitorAgent.Tests;

/// <summary>
/// AgentConfig.IsHubUrl decides whether a request carries this agent's bearer token, so
/// these are a security contract rather than a parsing nicety.
///
/// The look-alikes below are the cases the previous <c>url.StartsWith(HubBase)</c> test
/// accepted. Package payload URLs are operator-supplied by design (hub
/// packages.validate_source accepts any http/https address), so an operator holding only
/// `deploy_packages` could point a deployment at one of these and collect the agent
/// credentials of every machine in their scope.
/// </summary>
public class HubUrlTests
{
    // Pinned rather than read from AgentConfig.HubBase so the expectations below stay true
    // regardless of what FLEETHUB_HUB is set to on the machine running the tests.
    private const string Hub = "https://temp.arkeanos.net";

    private static void WithHub(string hubBase, Action body)
    {
        var previous = Environment.GetEnvironmentVariable("FLEETHUB_HUB");
        Environment.SetEnvironmentVariable("FLEETHUB_HUB", hubBase);
        try { body(); }
        finally { Environment.SetEnvironmentVariable("FLEETHUB_HUB", previous); }
    }

    [Theory]
    [InlineData(Hub + "/api/agent/packages/abc")]
    [InlineData(Hub + "/")]
    [InlineData("HTTPS://TEMP.ARKEANOS.NET/api/agent/packages/abc")]  // host case is irrelevant
    [InlineData(Hub + ":443/api/agent/packages/abc")]                  // explicit default port
    public void Our_own_hub_is_recognised(string url) =>
        WithHub(Hub, () => Assert.True(AgentConfig.IsHubUrl(url), url));

    [Theory]
    // A suffixed domain: the real host is attacker.net, but it starts with the hub string.
    [InlineData("https://temp.arkeanos.net.attacker.net/payload.exe")]
    // Userinfo: everything before the @ is a username, so the real host is attacker.net.
    [InlineData("https://temp.arkeanos.net@attacker.net/payload.exe")]
    [InlineData("https://temp.arkeanos.net:pw@attacker.net/payload.exe")]
    // A different scheme or port is a different origin, even on the right host.
    [InlineData("http://temp.arkeanos.net/payload.exe")]
    [InlineData("https://temp.arkeanos.net:8443/payload.exe")]
    // Plainly elsewhere.
    [InlineData("https://attacker.net/payload.exe")]
    [InlineData("file:///C:/Windows/System32/calc.exe")]
    public void Look_alike_and_foreign_hosts_are_refused(string url) =>
        WithHub(Hub, () => Assert.False(AgentConfig.IsHubUrl(url), url));

    [Theory]
    [InlineData("")]
    [InlineData("   ")]
    [InlineData(null)]
    [InlineData("not a url")]
    [InlineData("/api/agent/packages/abc")]   // relative: resolved before this is asked
    public void Junk_fails_closed(string? url) =>
        WithHub(Hub, () => Assert.False(AgentConfig.IsHubUrl(url)));

    [Fact]
    public void A_non_default_hub_base_is_honoured()
    {
        WithHub("http://localhost:3001", () =>
        {
            Assert.True(AgentConfig.IsHubUrl("http://localhost:3001/api/agent/packages/abc"));
            // Same host, different port -- a different service on the developer's box.
            Assert.False(AgentConfig.IsHubUrl("http://localhost:3002/api/agent/packages/abc"));
            Assert.False(AgentConfig.IsHubUrl("http://localhost.attacker.net:3001/x"));
        });
    }
}
