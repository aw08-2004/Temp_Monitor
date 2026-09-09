namespace FleetHubAgent.Core.Tests;

/// <summary>
/// Holds the two properties that decide where this agent's credentials go.
///
/// IsHubUrl is what a payload-fetching executor asks before attaching the bearer token. A
/// StartsWith(HubBase) test -- the obvious implementation, and the one this replaced in the
/// Windows agent -- says yes to https://hub.example.com.attacker.net/x, whose real host is
/// attacker.net. Nothing calls it yet; it is tested now so the first caller inherits a
/// correct one rather than writing the StartsWith version next to it.
///
/// Configure is the Android-only half: a hub URL arrives from an MDM's managed configuration
/// or from the setup screen, and a blank or malformed one must not silently redirect a device
/// to nowhere. **These tests mutate global state**, so they are serialised -- see the
/// collection attribute.
/// </summary>
[Collection(nameof(HubUrlTests))]
[CollectionDefinition(nameof(HubUrlTests), DisableParallelization = true)]
public class HubUrlTests : IDisposable
{
    public HubUrlTests() => AgentConfig.Configure(AgentConfig.DefaultHubBase);
    public void Dispose() => AgentConfig.Configure(AgentConfig.DefaultHubBase);

    [Fact]
    public void The_hubs_own_url_is_recognised()
    {
        Assert.True(AgentConfig.IsHubUrl(AgentConfig.HubBase + "/api/agent/commands"));
    }

    [Theory]
    [InlineData("https://your.hub.url.attacker.net/x")]  // suffix, not the same host
    [InlineData("https://your.hub.url@attacker.net/x")]  // userinfo -- real host is attacker.net
    [InlineData("http://your.hub.url/x")]                // scheme differs
    [InlineData("https://your.hub.url:8443/x")]          // port differs
    [InlineData("not a url")]
    [InlineData("")]
    [InlineData(null)]
    public void Anything_that_is_not_our_origin_is_refused(string? url)
    {
        Assert.False(AgentConfig.IsHubUrl(url));
    }

    [Fact]
    public void Configure_points_the_agent_at_a_new_hub()
    {
        AgentConfig.Configure("https://hub.example.com");

        Assert.Equal("https://hub.example.com", AgentConfig.HubBase);
        Assert.Equal("https://hub.example.com/api/report", AgentConfig.ReportUrl);
        Assert.True(AgentConfig.IsHubUrl("https://hub.example.com/api/agent/commands"));
    }

    [Fact]
    public void Configure_strips_a_trailing_slash_rather_than_doubling_it()
    {
        AgentConfig.Configure("https://hub.example.com/");
        Assert.Equal("https://hub.example.com/api/report", AgentConfig.ReportUrl);
    }

    [Theory]
    [InlineData("")]
    [InlineData("   ")]
    [InlineData(null)]
    [InlineData("hub.example.com")]        // no scheme -- not absolute
    [InlineData("ftp://hub.example.com")]  // a scheme we would never speak
    public void An_unusable_value_leaves_the_agent_where_it_was(string? value)
    {
        AgentConfig.Configure(value);
        // An agent pointed at nothing reports nowhere and looks, from the device, exactly like
        // one that is working.
        Assert.Equal(AgentConfig.DefaultHubBase, AgentConfig.HubBase);
    }

    [Fact]
    public void Every_endpoint_hangs_off_the_configured_base()
    {
        AgentConfig.Configure("https://hub.example.com");

        Assert.StartsWith("https://hub.example.com/", AgentConfig.EnrollUrl);
        Assert.StartsWith("https://hub.example.com/", AgentConfig.HeartbeatUrl);
        Assert.StartsWith("https://hub.example.com/", AgentConfig.CommandsUrl);
        Assert.StartsWith("https://hub.example.com/", AgentConfig.CommandResultUrl("abc"));
    }

    [Fact]
    public void A_command_id_is_escaped_into_the_result_url()
    {
        // Ids come from the hub, but this is the one place a value is pasted into a path.
        Assert.Contains("a%2Fb", AgentConfig.CommandResultUrl("a/b"));
    }

    [Fact]
    public void A_held_poll_asks_for_the_wait_it_means()
    {
        Assert.EndsWith("?wait=25", AgentConfig.CommandsUrl_Waiting(25));
    }
}
