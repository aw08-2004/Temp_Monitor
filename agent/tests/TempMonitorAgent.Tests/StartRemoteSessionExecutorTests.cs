using System.Text.Json.Nodes;
using TempMonitorAgent.Fleet;
using TempMonitorAgent.Remote;

namespace TempMonitorAgent.Tests;

/// <summary>
/// The testable seams of the remote-session launch (roadmap #2). The injection itself needs
/// SYSTEM + an interactive desktop and is validated on-device; what is pinned here is the part
/// that decides WHAT gets launched: param parsing, the trusted-attribution rule, and the
/// session-id -> filename safety check (a bad id becomes a path).
/// </summary>
public class StartRemoteSessionExecutorTests
{
    private static FleetCommand Command(JsonObject? paramsObj, string issuedBy = "op@example.com") => new()
    {
        Id = "cmd-1",
        Type = "start_remote_session",
        Params = paramsObj,
        IssuedBy = issuedBy,
    };

    [Fact]
    public void BuildSessionParams_ParsesValidCommand()
    {
        var (session, error) = StartRemoteSessionExecutor.BuildSessionParams(
            Command(new JsonObject
            {
                ["session_id"] = "abc123DEF",
                ["monitor"] = 2,
                ["consent_mode"] = "attended",
            }));

        Assert.Null(error);
        Assert.NotNull(session);
        Assert.Equal("abc123DEF", session!.SessionId);
        Assert.Equal(2, session.Monitor);
        Assert.Equal("attended", session.ConsentMode);
    }

    [Fact]
    public void BuildSessionParams_DefaultsMonitorAndConsent()
    {
        var (session, error) = StartRemoteSessionExecutor.BuildSessionParams(
            Command(new JsonObject { ["session_id"] = "s1" }));

        Assert.Null(error);
        Assert.Equal(0, session!.Monitor);
        Assert.Equal("unattended", session.ConsentMode);
    }

    [Fact]
    public void BuildSessionParams_IssuedByComesFromCommandNotParams()
    {
        // A client-supplied issued_by in params must never win over the hub's trusted
        // attribution -- otherwise one operator could start a session as another.
        var (session, _) = StartRemoteSessionExecutor.BuildSessionParams(
            Command(new JsonObject
            {
                ["session_id"] = "s1",
                ["issued_by"] = "attacker@example.com",
            }, issuedBy: "real@example.com"));

        Assert.Equal("real@example.com", session!.IssuedBy);
    }

    [Fact]
    public void BuildSessionParams_RejectsMissingSessionId()
    {
        var (session, error) = StartRemoteSessionExecutor.BuildSessionParams(
            Command(new JsonObject { ["monitor"] = 0 }));

        Assert.Null(session);
        Assert.Contains("session_id", error);
    }

    [Fact]
    public void BuildSessionParams_ClampsNegativeMonitor()
    {
        var (session, _) = StartRemoteSessionExecutor.BuildSessionParams(
            Command(new JsonObject { ["session_id"] = "s1", ["monitor"] = -5 }));

        Assert.Equal(0, session!.Monitor);
    }

    [Theory]
    [InlineData("abc123", true)]
    [InlineData("a-b_c-9", true)]
    [InlineData("DEADBEEFdeadbeef", true)]
    [InlineData("", false)]
    [InlineData("../etc", false)]
    [InlineData("a b", false)]
    [InlineData("a/b", false)]
    [InlineData("a.b", false)]      // '.' would allow a traversal component
    [InlineData("a\\b", false)]
    public void IsSafeSessionId_RejectsPathMetacharacters(string id, bool expected)
    {
        Assert.Equal(expected, StartRemoteSessionExecutor.IsSafeSessionId(id));
    }

    [Fact]
    public void BuildSessionParams_RejectsUnsafeSessionId()
    {
        var (session, error) = StartRemoteSessionExecutor.BuildSessionParams(
            Command(new JsonObject { ["session_id"] = "../../evil" }));

        Assert.Null(session);
        Assert.Contains("session_id", error);
    }

    [Fact]
    public void SessionFilePath_StaysUnderRemoteStateDir()
    {
        var path = StartRemoteSessionExecutor.SessionFilePath("abc123");
        Assert.StartsWith(AgentConfig.RemoteStateDir, path);
        Assert.EndsWith("abc123.session.json", path);
    }

    [Fact]
    public void BuildSessionParams_ParsesIceServers()
    {
        var (session, _) = StartRemoteSessionExecutor.BuildSessionParams(
            Command(new JsonObject
            {
                ["session_id"] = "s1",
                ["ice_servers"] = new JsonArray(
                    new JsonObject { ["urls"] = new JsonArray("stun:stun.example:3478") },
                    new JsonObject
                    {
                        ["urls"] = new JsonArray("turn:hub.example:3478"),
                        ["username"] = "expiry:s1",
                        ["credential"] = "pw",
                    }),
            }));

        Assert.Equal(2, session!.IceServers.Count);
        Assert.Equal("stun:stun.example:3478", session.IceServers[0].Urls[0]);
        Assert.Equal("expiry:s1", session.IceServers[1].Username);
        Assert.Equal("pw", session.IceServers[1].Credential);
    }

    [Fact]
    public void BuildSessionParams_MissingIceServersYieldsEmptyList()
    {
        var (session, _) = StartRemoteSessionExecutor.BuildSessionParams(
            Command(new JsonObject { ["session_id"] = "s1" }));
        Assert.Empty(session!.IceServers);
    }
}
