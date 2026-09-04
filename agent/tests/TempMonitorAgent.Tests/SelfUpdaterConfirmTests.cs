using Microsoft.Extensions.Logging.Abstractions;
using TempMonitorAgent.State;
using TempMonitorAgent.Update;

namespace TempMonitorAgent.Tests;

/// <summary>
/// When an applied update stops being provisional.
///
/// THE SILENT FAILURE THIS FILE EXISTS TO CATCH is the one the restart guard was
/// previously retired by: ReconcileAfterBoot used to treat "the process started" as "the
/// update worked", clearing the guard and deleting the previous binary on the first boot
/// of the new build. A build that starts and only then turns out to be unable to reach the
/// hub is a machine nobody can reach, fix or tell anything -- and because every machine
/// takes the same signed manifest, that is the whole fleet on the same afternoon. The
/// rollback artifact was being destroyed at the first moment it was safe to destroy it,
/// which is not the same moment it stopped being needed.
///
/// Nothing exercised any of this before. These tests fail against the old behaviour (the
/// guard is gone after ReconcileAfterBoot) and pass against the current one, which is the
/// only property worth pinning here.
///
/// **What is deliberately not covered: the .old binary itself.** TryDeleteOldBinary is
/// keyed to Environment.ProcessPath, which under a test run is the test host -- so
/// asserting on it would mean creating and deleting files beside whatever binary happens
/// to be running the suite. Testing it properly needs a seam for that path, which is worth
/// adding the day this class grows a third branch, and is not worth writing files into an
/// SDK directory for today. The restart guard is written and cleared in lockstep with the
/// binary by the same two methods, so it is a faithful proxy for when each happens.
/// </summary>
public sealed class SelfUpdaterConfirmTests
{
    // AssemblySetup points AgentConfig.ProgramDataDir -- and so RestartStatePath -- at a
    // scratch tree for the whole run, which is what lets these touch real files.
    private static SelfUpdater NewUpdater(AgentState state) =>
        new(NullLogger<SelfUpdater>.Instance, state);

    private static AgentState FreshState()
    {
        var state = new AgentState();
        state.ClearRestartState();
        return state;
    }

    [Fact]
    public void BootingOntoTheTargetDoesNotRetireTheGuard()
    {
        var state = FreshState();
        state.SaveRestartState(new RestartState { Target = AgentConfig.Version, Count = 1 });

        NewUpdater(state).ReconcileAfterBoot();

        var after = state.LoadRestartState();
        Assert.NotNull(after);
        Assert.Equal(AgentConfig.Version, after!.Target);
        Assert.Equal(1, after.Count);
    }

    [Fact]
    public void ConfirmingRetiresTheGuard()
    {
        var state = FreshState();
        state.SaveRestartState(new RestartState { Target = AgentConfig.Version, Count = 1 });

        var updater = NewUpdater(state);
        updater.ReconcileAfterBoot();
        updater.ConfirmRunningBuild();

        Assert.Null(state.LoadRestartState());
    }

    [Fact]
    public void ConfirmingWithoutHavingBootedOntoAnUpdateChangesNothing()
    {
        // The ordinary case: no update is pending, and the telemetry loop calls this on
        // every single report. It must not touch state it was never told about -- a guard
        // left by an update that has NOT yet booted onto its target is not this build's to
        // clear.
        var state = FreshState();
        state.SaveRestartState(new RestartState { Target = "999.0.0", Count = 2 });

        var updater = NewUpdater(state);
        updater.ReconcileAfterBoot();   // running version is below the target: not confirmed
        updater.ConfirmRunningBuild();

        var after = state.LoadRestartState();
        Assert.NotNull(after);
        Assert.Equal("999.0.0", after!.Target);
        Assert.Equal(2, after.Count);
    }

    [Fact]
    public void ConfirmingIsIdempotentAndCheapAfterTheFirstCall()
    {
        // Called on every report for the life of the process, so calling it again after it
        // has done its work must be a no-op rather than a second attempt at the files.
        var state = FreshState();
        state.SaveRestartState(new RestartState { Target = AgentConfig.Version, Count = 1 });

        var updater = NewUpdater(state);
        updater.ReconcileAfterBoot();
        updater.ConfirmRunningBuild();
        state.SaveRestartState(new RestartState { Target = "999.0.0", Count = 5 });
        updater.ConfirmRunningBuild();

        // The second call must not have cleared the guard a LATER update just wrote.
        var after = state.LoadRestartState();
        Assert.NotNull(after);
        Assert.Equal("999.0.0", after!.Target);
    }
}
