using Microsoft.Extensions.Logging.Abstractions;
using FleetHubAgent.Fleet;
using FleetHubAgent.Fleet.Executors;

namespace FleetHubAgent.Core.Tests;

/// <summary>
/// Holds the two shapes of "this agent did not run your command" apart.
///
/// The distinction is the whole reason CommandDispatcher has a table rather than one message,
/// and it is easy to lose in a later edit that "simplifies" the two branches into one. An
/// operator told a reboot is "not implemented" waits for a version of this agent that does it;
/// there is not going to be one, because Android has no reboot API for an ordinary app. An
/// operator told a command is "not implemented" when it merely has not been written yet is
/// being told the truth.
/// </summary>
public class DispatcherTests
{
    private sealed class StubExecutor(string type, CommandResult result) : ICommandExecutor
    {
        public string Type => type;
        public Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput, CancellationToken ct) =>
            Task.FromResult(result);
    }

    private sealed class ThrowingExecutor : ICommandExecutor
    {
        public string Type => "explodes";
        public Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput, CancellationToken ct) =>
            throw new InvalidOperationException("boom");
    }

    private static CommandDispatcher Build(params ICommandExecutor[] executors) =>
        new(NullLogger<CommandDispatcher>.Instance, executors);

    [Fact]
    public void A_platform_impossible_command_says_it_cannot_be_run_not_that_it_is_missing()
    {
        var message = CommandDispatcher.Unsupported("run_script", ["rename"]);
        Assert.Contains("cannot be run on Android", message);
        Assert.DoesNotContain("not implemented by", message);
        // The reason, not just the refusal -- an operator who knows why does not open a ticket.
        Assert.Contains("no shell", message);
    }

    [Fact]
    public void Restart_names_the_route_that_does_exist()
    {
        // There IS an answer for reboot, and it is not this agent. Saying so is the difference
        // between a dead end and a next step.
        Assert.Contains("MDM", CommandDispatcher.Unsupported("restart", ["rename"]));
    }

    [Theory]
    [InlineData("backup_files")]
    [InlineData("restore_files")]
    [InlineData("install_patches")]
    [InlineData("deploy_package")]
    public void Scheduler_driven_commands_are_named_as_impossible_not_merely_unwritten(string type)
    {
        // These reach an Android device even though no console button offers them: they are
        // hub/fleet.py's SCHEDULED_COMMANDS, dispatched by the scheduler and rules engine
        // against a machine set, and the MIN_*_AGENT gates are console-side JavaScript that
        // never runs for them. The first real device was in a fleet-wide backup profile and had
        // backup_files queued within a minute of enrolling, which is how this was found.
        //
        // An operator reading a nightly failure needs "cannot, and here is what to change",
        // not "not implemented yet".
        var message = CommandDispatcher.Unsupported(type, ["rename"]);
        Assert.Contains("cannot be run on Android", message);
        Assert.DoesNotContain("not implemented by", message);
    }

    [Fact]
    public void The_backup_message_says_what_to_change_on_the_hub()
    {
        // The fix is not on the device -- it is scoping the profile so it stops targeting a
        // machine that can never answer. Saying so is what turns a recurring failure into a
        // one-time action.
        Assert.Contains("Exclude", CommandDispatcher.Unsupported("backup_files", ["rename"]));
    }

    [Fact]
    public void An_unwritten_command_reads_as_not_implemented_and_names_the_version()
    {
        // show_message rather than gpupdate: it is the honest example of this branch, because
        // a phone CAN show a message and this agent simply has not implemented it yet. gpupdate
        // used to stand here and had to move -- Group Policy has no Android counterpart, so it
        // belongs in the impossible table, and using it here tested the wrong thing.
        var message = CommandDispatcher.Unsupported("show_message", ["rename"]);
        Assert.Contains("not implemented by the Android agent", message);
        Assert.Contains(AgentConfig.Version, message);
    }

    [Fact]
    public void Both_shapes_list_what_this_agent_can_do()
    {
        Assert.Contains("rename", CommandDispatcher.Unsupported("shutdown", ["rename"]));
        Assert.Contains("rename", CommandDispatcher.Unsupported("show_message", ["rename"]));
    }

    [Fact]
    public async Task An_unknown_type_still_produces_a_result()
    {
        // Never an exception and never silence: the hub shows a command with no result as
        // running forever.
        var result = await Build(new StubExecutor("rename", CommandResult.Ok()))
            .ExecuteAsync(new FleetCommand { Id = "1", Type = "install_bios" }, null, default);

        Assert.False(result.Success);
        Assert.Contains("install_bios", result.Output);
    }

    [Fact]
    public async Task An_executor_that_throws_is_reported_rather_than_escaping()
    {
        var result = await Build(new ThrowingExecutor())
            .ExecuteAsync(new FleetCommand { Id = "1", Type = "explodes" }, null, default);

        Assert.False(result.Success);
        Assert.Contains("boom", result.Output);
    }

    [Fact]
    public async Task Output_is_truncated_to_what_the_hub_will_store()
    {
        var huge = new string('x', 40_000);
        var result = await Build(new StubExecutor("rename", CommandResult.Ok(huge)))
            .ExecuteAsync(new FleetCommand { Id = "1", Type = "rename" }, null, default);

        Assert.True(result.Output!.Length < huge.Length);
        Assert.EndsWith("(truncated)", result.Output);
    }
}
