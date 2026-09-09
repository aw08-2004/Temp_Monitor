using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging.Abstractions;
using FleetHubAgent.Fleet;
using FleetHubAgent.Fleet.Executors;

namespace FleetHubAgent.Core.Tests;

/// <summary>
/// Remote lock and remote wipe -- roadmap #23 phase H.
///
/// **The silent failure this file exists to catch is a wipe that is never answered.**
/// `wipeData` does not return: the process dies, the data partition goes, and nothing on that
/// device ever speaks to the hub again. An executor that called it inline would leave a command
/// claimed and unanswered forever, beside a machine that stopped reporting -- which in the
/// console is indistinguishable from a flat battery. So the result is produced BEFORE the erase
/// is armed, and that ordering is asserted here rather than trusted, because the only place it
/// could be discovered otherwise is a real device that cannot be un-wiped.
///
/// The second is the opposite of LocateDeviceExecutor's rule, and getting the two the same way
/// round would be the bug. A device that cannot give a position has answered truthfully and its
/// result is a SUCCESS with a reason. A device that cannot lock or wipe has not done what was
/// asked, and somebody hoping a lost phone is now locked must not read "done" -- so an
/// unmanaged device FAILS.
///
/// The third is the default for factory-reset protection. Absent from the params -- an older
/// console, a hand-rolled command -- must mean CLEAR it, matching hub/wipe_web.py's default,
/// because the two disagreeing produces a device that is wiped and then cannot be set up again
/// by the organisation that owns it.
/// </summary>
public class DeviceSecurityTests
{
    private sealed class FakeSecurity(bool canEnforce = true) : IDeviceSecurity
    {
        public bool CanEnforce { get; set; } = canEnforce;
        public bool LockRefuses { get; set; }
        public Exception? LockThrows { get; set; }
        public int Locks { get; private set; }

        private readonly TaskCompletionSource<bool> _wiped = new();
        /// <summary>Completes with the reset-protection flag the moment Wipe is called.</summary>
        public Task<bool> Wiped => _wiped.Task;
        public bool WipeCalled => _wiped.Task.IsCompleted;

        public bool Lock()
        {
            if (LockThrows is not null) throw LockThrows;
            Locks++;
            return !LockRefuses;
        }

        public void Wipe(bool clearResetProtection) => _wiped.TrySetResult(clearResetProtection);
    }

    private static FleetCommand Command(string type, JsonObject? parameters = null)
        => new() { Id = "cmd-1", Type = type, IssuedBy = "super@x.com", Params = parameters };

    private static WipeDeviceExecutor Wiper(FakeSecurity security)
        // Milliseconds rather than the shipped ten seconds: the assertion is about ORDER, not
        // about duration, and a test that waited out the real delay would be ten seconds long
        // for nothing.
        => new(NullLogger<WipeDeviceExecutor>.Instance, security, TimeSpan.FromMilliseconds(50));

    // ---------------------------------------------------------------- lock

    [Fact]
    public async Task Locking_an_unmanaged_device_FAILS_rather_than_reporting_success()
    {
        var security = new FakeSecurity(canEnforce: false);
        var executor = new LockDeviceExecutor(NullLogger<LockDeviceExecutor>.Instance, security);

        var result = await executor.ExecuteAsync(Command("lock_device"), null, default);

        Assert.False(result.Success);
        Assert.Contains("not fully managed", result.Output);
        Assert.Equal(0, security.Locks);
    }

    [Fact]
    public async Task A_managed_device_locks()
    {
        var security = new FakeSecurity();
        var executor = new LockDeviceExecutor(NullLogger<LockDeviceExecutor>.Instance, security);

        var result = await executor.ExecuteAsync(Command("lock_device"), null, default);

        Assert.True(result.Success);
        Assert.Equal(1, security.Locks);
    }

    [Fact]
    public async Task A_refused_lock_is_a_failure_and_says_nothing_changed()
    {
        var security = new FakeSecurity { LockRefuses = true };
        var executor = new LockDeviceExecutor(NullLogger<LockDeviceExecutor>.Instance, security);

        var result = await executor.ExecuteAsync(Command("lock_device"), null, default);

        Assert.False(result.Success);
        Assert.Contains("Nothing has changed", result.Output);
    }

    [Fact]
    public async Task A_platform_that_throws_is_answered_rather_than_taking_the_loop_down()
    {
        var security = new FakeSecurity { LockThrows = new InvalidOperationException("boom") };
        var executor = new LockDeviceExecutor(NullLogger<LockDeviceExecutor>.Instance, security);

        var result = await executor.ExecuteAsync(Command("lock_device"), null, default);

        Assert.False(result.Success);
        Assert.Contains("boom", result.Output);
    }

    // ---------------------------------------------------------------- wipe

    [Fact]
    public async Task Wiping_an_unmanaged_device_FAILS_and_erases_nothing()
    {
        var security = new FakeSecurity(canEnforce: false);
        var result = await Wiper(security).ExecuteAsync(Command("wipe_device"), null, default);

        Assert.False(result.Success);
        Assert.Contains("not fully managed", result.Output);
        Assert.False(security.WipeCalled);
    }

    [Fact]
    public async Task The_result_comes_back_BEFORE_the_device_is_erased()
    {
        // The assertion this file exists for. If the erase were inline, this call would never
        // return on a real device and the command would stay claimed forever.
        var security = new FakeSecurity();
        var result = await Wiper(security).ExecuteAsync(Command("wipe_device"), null, default);

        Assert.True(result.Success);
        Assert.False(security.WipeCalled);        // answered first...
        Assert.True(await security.Wiped);        // ...and only then armed
    }

    [Fact]
    public async Task Factory_reset_protection_is_CLEARED_when_the_hub_says_nothing()
    {
        // Must match hub/wipe_web.py's default. The two disagreeing gives a company-owned
        // device that is wiped and then cannot be set up again by the company that owns it.
        var security = new FakeSecurity();
        await Wiper(security).ExecuteAsync(Command("wipe_device"), null, default);
        Assert.True(await security.Wiped);
    }

    [Fact]
    public async Task Factory_reset_protection_is_KEPT_when_the_hub_asks_for_that()
    {
        var security = new FakeSecurity();
        var parameters = new JsonObject { ["reset_protection"] = false };
        var result = await Wiper(security).ExecuteAsync(
            Command("wipe_device", parameters), null, default);

        Assert.False(await security.Wiped);
        // The operator is told which of the two happened, because the consequences differ and
        // one of them is a device nobody can set up again.
        Assert.Contains("cannot be set up again", result.Output);
    }

    [Fact]
    public void The_command_types_match_the_hub()
    {
        // hub/wipe.py's COMMAND_FOR. A mismatch here is a command the dispatcher routes
        // nowhere, answered "unsupported" to a console that shows the button.
        Assert.Equal("lock_device",
            new LockDeviceExecutor(NullLogger<LockDeviceExecutor>.Instance,
                                   new FakeSecurity()).Type);
        Assert.Equal("wipe_device", Wiper(new FakeSecurity()).Type);
    }

    [Fact]
    public void Both_are_reported_as_capabilities_so_the_hub_stops_refusing_them()
    {
        // The hub's create_command refuses a command type this agent has not claimed, and the
        // claim is DERIVED from the dispatcher -- so an executor added without being registered
        // would leave the hub refusing a command the device can now run.
        var security = new FakeSecurity();
        var dispatcher = new CommandDispatcher(
            NullLogger<CommandDispatcher>.Instance,
            [
                new LockDeviceExecutor(NullLogger<LockDeviceExecutor>.Instance, security),
                Wiper(security),
            ]);
        var report = AgentCapabilities.For(dispatcher);

        Assert.Contains("lock_device", report.Commands);
        Assert.Contains("wipe_device", report.Commands);
    }
}
