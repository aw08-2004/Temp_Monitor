using TempMonitorAgent.Remote;

namespace TempMonitorAgent.Tests;

/// <summary>Argument handling for the session-injected helper (roadmap #2): a normal service
/// launch and a helper launch travel through the same binary and must be told apart before
/// the service host is built, and the session parameters must survive the file round-trip the
/// executor and helper use to exchange them (deliberately not the command line).</summary>
public class RemoteHelperTests
{
    [Fact]
    public void TryGetSessionFileArg_ReturnsNullForNormalLaunch()
    {
        Assert.Null(RemoteHelper.TryGetSessionFileArg(Array.Empty<string>()));
        Assert.Null(RemoteHelper.TryGetSessionFileArg(new[] { "--some-other-flag", "x" }));
    }

    [Fact]
    public void TryGetSessionFileArg_ReturnsPathAfterFlag()
    {
        var path = RemoteHelper.TryGetSessionFileArg(
            new[] { AgentConfig.RemoteHelperArg, @"C:\ProgramData\FleetHub\Agent\remote\s1.session.json" });
        Assert.Equal(@"C:\ProgramData\FleetHub\Agent\remote\s1.session.json", path);
    }

    [Fact]
    public void TryGetSessionFileArg_ReturnsEmptyWhenFlagHasNoValue()
    {
        // Non-null (so Program.cs takes the helper branch) but empty, so Run reports the
        // missing-file error rather than silently starting the service.
        Assert.Equal("", RemoteHelper.TryGetSessionFileArg(new[] { AgentConfig.RemoteHelperArg }));
    }

    [Fact]
    public void RemoteSessionParams_RoundTripsThroughJson()
    {
        var original = new RemoteSessionParams
        {
            SessionId = "abc123",
            Monitor = 1,
            ConsentMode = "attended",
            IssuedBy = "op@example.com",
        };

        var restored = RemoteSessionParams.FromJson(original.ToJson());

        Assert.NotNull(restored);
        Assert.Equal(original.SessionId, restored!.SessionId);
        Assert.Equal(original.Monitor, restored.Monitor);
        Assert.Equal(original.ConsentMode, restored.ConsentMode);
        Assert.Equal(original.IssuedBy, restored.IssuedBy);
    }

    [Fact]
    public void RemoteSessionParams_IgnoresUnknownMembers()
    {
        // Forward compatibility: a newer hub adds fields (e.g. signaling_url) that an older
        // helper must read past without throwing. ice_servers is well-formed (urls is always a
        // list, as the hub emits and the executor re-serializes it).
        var restored = RemoteSessionParams.FromJson(
            """{"session_id":"s1","monitor":0,"signaling_url":"https://x","ice_servers":[{"urls":["turn:x"],"username":"u","credential":"p"}]}""");

        Assert.NotNull(restored);
        Assert.Equal("s1", restored!.SessionId);
        Assert.Single(restored.IceServers);
        Assert.Equal("turn:x", restored.IceServers[0].Urls[0]);
    }

    [Fact]
    public void RemoteSessionParams_FromJson_ReturnsNullOnGarbage()
    {
        Assert.Null(RemoteSessionParams.FromJson("not json"));
    }
}
