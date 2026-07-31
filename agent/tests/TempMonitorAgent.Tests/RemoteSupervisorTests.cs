using System.Text.Json;
using TempMonitorAgent.Remote;
using Xunit;

namespace TempMonitorAgent.Tests;

/// <summary>
/// The rules that decide whether a live remote session follows the interactive session when it
/// moves (sign-out, switch user, RDP taking the console). The decision is pure so these can run
/// without a Windows session, a helper process, or a state directory -- the parts that need
/// those are the kill and the re-injection, which are exercised on real hardware.
/// </summary>
public class RemoteSupervisorTests
{
    private const uint None = 0xFFFFFFFF;

    [Fact]
    public void PinnedSession_NeverFollowsTheConsole()
    {
        // The operator asked for session 3. Session 5 owning the console now is not a reason to
        // abandon what they asked for -- watching one user's session while another uses the
        // machine is a legitimate thing to be doing.
        Assert.Equal(RemoteSessionSupervisor.MoveDecision.Stay,
                     RemoteSessionSupervisor.DecideMove(
                         auto: false, current: 3, target: 5, pending: 5, moves: 0));
    }

    [Fact]
    public void UnchangedSelection_StaysPut()
    {
        Assert.Equal(RemoteSessionSupervisor.MoveDecision.Stay,
                     RemoteSessionSupervisor.DecideMove(
                         auto: true, current: 2, target: 2, pending: null, moves: 0));
    }

    [Fact]
    public void NothingInteractiveAnywhere_StaysPut()
    {
        // A normal beat or two mid-sign-out. Moving "nowhere" would kill a helper that is about
        // to be perfectly fine.
        Assert.Equal(RemoteSessionSupervisor.MoveDecision.Stay,
                     RemoteSessionSupervisor.DecideMove(
                         auto: true, current: 2, target: None, pending: null, moves: 0));
    }

    [Fact]
    public void ANewTarget_IsWaitedOutOnceBeforeMoving()
    {
        // First sighting: remember it, do nothing.
        Assert.Equal(RemoteSessionSupervisor.MoveDecision.Wait,
                     RemoteSessionSupervisor.DecideMove(
                         auto: true, current: 1, target: 4, pending: null, moves: 0));

        // Same answer next pass: the switch has settled, move.
        Assert.Equal(RemoteSessionSupervisor.MoveDecision.Move,
                     RemoteSessionSupervisor.DecideMove(
                         auto: true, current: 1, target: 4, pending: 4, moves: 0));
    }

    [Fact]
    public void ASelectionStillWobbling_KeepsWaiting()
    {
        // Windows moved us from "session 3 next" to "session 4 next" between passes -- it has
        // not settled, so neither do we.
        Assert.Equal(RemoteSessionSupervisor.MoveDecision.Wait,
                     RemoteSessionSupervisor.DecideMove(
                         auto: true, current: 1, target: 4, pending: 3, moves: 0));
    }

    [Fact]
    public void RepeatedMoves_AreCappedRatherThanThrashed()
    {
        Assert.Equal(RemoteSessionSupervisor.MoveDecision.Move,
                     RemoteSessionSupervisor.DecideMove(
                         auto: true, current: 1, target: 2, pending: 2, moves: 9));
        Assert.Equal(RemoteSessionSupervisor.MoveDecision.Capped,
                     RemoteSessionSupervisor.DecideMove(
                         auto: true, current: 1, target: 2, pending: 2, moves: 10));
    }

    [Fact]
    public void Record_RoundTripsTheSessionItIsFollowing()
    {
        var record = new RemoteSessionSupervisor.LiveSessionRecord
        {
            SessionId = "abc123",
            Params = "{\"session_id\":\"abc123\"}",
            Pid = 4242,
            WindowsSession = 7,
            Auto = true,
            SessionMoves = 2,
            StartedAt = DateTimeOffset.UtcNow,
        };

        var back = JsonSerializer.Deserialize<RemoteSessionSupervisor.LiveSessionRecord>(
            JsonSerializer.Serialize(record))!;

        Assert.Equal(7u, back.WindowsSession);
        Assert.True(back.Auto);
        Assert.Equal(2, back.SessionMoves);
        Assert.Equal(4242, back.Pid);
    }

    [Fact]
    public void ARecordWrittenByTheOlderAgent_DoesNotFollowAnything()
    {
        // A session already in flight across an agent update has no auto flag. Defaulting to
        // "pinned" leaves it exactly as it behaved before the update rather than moving a helper
        // on the strength of a field nobody wrote.
        var back = JsonSerializer.Deserialize<RemoteSessionSupervisor.LiveSessionRecord>(
            """{"session_id":"old","params":"{}","pid":10,"started_at":"2026-07-31T00:00:00+00:00"}""")!;

        Assert.False(back.Auto);
        Assert.Equal(0u, back.WindowsSession);
        Assert.Equal(RemoteSessionSupervisor.MoveDecision.Stay,
                     RemoteSessionSupervisor.DecideMove(
                         back.Auto, back.WindowsSession, target: 5, pending: 5, moves: 0));
    }
}
