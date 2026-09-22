using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging.Abstractions;
using FleetHubAgent.Fleet.Executors;

namespace FleetHubAgent.Core.Tests;

/// <summary>
/// The message command, and the four ways it would fail without anybody noticing.
///
/// **An outcome the hub cannot route is recorded and silently does nothing.** That is
/// rules.handle_message_result's deliberate behaviour -- an unmapped outcome is an explicit
/// no-op so "they pressed No and nothing happened" stays visible in the history. Which means
/// an agent that invented an outcome string would produce a rule that looks configured,
/// answered and dead, with nothing failing at either end. So every outcome that leaves this
/// executor is asserted to be one the hub knows.
///
/// **A message nobody could be shown must be a SUCCESS.** The `no_session` route is how an
/// operator says "then ask again in half an hour", and reporting it as a failed command both
/// strands that route and makes every rule's history look broken. Same line LocateDeviceExecutor
/// draws for a device that cannot see the sky.
///
/// **A fourth button on a three-button surface is a rule branch nobody can ever reach.**
/// Android draws at most three notification actions; truncating would leave the hub holding a
/// route that can only ever time out. The refusal names the limit because the operator is the
/// one who has to change something.
///
/// **And the wait is never infinite here**, unlike the Windows executor's. A waiting message
/// holds one of AgentConfig.MaxConcurrentCommands slots, so an unanswered indefinite message
/// is a phone that quietly stops accepting commands while showing as online.
/// </summary>
public class ShowMessageExecutorTests
{
    private sealed class StubPresenter(PromptAnswer answer) : IUserMessagePresenter
    {
        public UserMessage? Shown { get; private set; }
        public bool CanReachAnybody { get; init; } = true;
        public string? UnreachableReason { get; init; }
        public int MaxButtons { get; init; } = 3;

        public Task<PromptAnswer> AskAsync(UserMessage message, CancellationToken ct)
        {
            Shown = message;
            return Task.FromResult(answer);
        }
    }

    private sealed class ThrowingPresenter : IUserMessagePresenter
    {
        public bool CanReachAnybody => true;
        public string? UnreachableReason => null;
        public int MaxButtons => 3;
        public Task<PromptAnswer> AskAsync(UserMessage message, CancellationToken ct) =>
            throw new InvalidOperationException("the notification manager exploded");
    }

    private static ShowMessageExecutor Build(IUserMessagePresenter presenter) =>
        new(NullLogger<ShowMessageExecutor>.Instance, presenter);

    private static JsonObject Params(params string[] buttonIds)
    {
        var buttons = new JsonArray();
        foreach (var id in buttonIds) buttons.Add(new JsonObject { ["id"] = id });
        return new JsonObject
        {
            ["title"] = "Server restart",
            ["body"] = "The file server restarts in ten minutes.",
            ["buttons"] = buttons,
        };
    }

    private static FleetCommand Command(JsonObject? parameters) =>
        new() { Id = "c1", Type = "show_message", Params = parameters, IssuedBy = "op@x.com" };

    private static JsonNode Answer(CommandResult result) =>
        JsonNode.Parse(result.Output ?? "")
        ?? throw new InvalidOperationException("the result was not JSON");

    [Fact]
    public void The_type_matches_the_hubs_command_type()
    {
        // hub/fleet.py's USER_MESSAGE_COMMANDS. A mismatch routes the command nowhere and
        // answers "unsupported" to a console that is showing the button.
        Assert.Equal("show_message", Build(new StubPresenter(new PromptAnswer("ok"))).Type);
    }

    [Fact]
    public async Task A_pressed_button_comes_back_under_the_keys_the_hub_reads()
    {
        // `outcome` is the only key rules.handle_message_result routes on, and Python cannot
        // import C#: renaming it would not crash anything, it would file every answer as a
        // timeout forever with nothing in any log.
        var executor = Build(new StubPresenter(new PromptAnswer("later")));
        var result = await executor.ExecuteAsync(Command(Params("yes", "no", "later")), null, default);

        Assert.True(result.Success);
        var answer = Answer(result);
        Assert.Equal("later", (string?)answer["outcome"]);
        Assert.NotNull(answer["shown_at"]);
        Assert.NotNull(answer["responded_at"]);
    }

    [Theory]
    [InlineData("timeout")]
    [InlineData("dismissed")]
    [InlineData("failed")]
    public async Task The_outcomes_that_are_not_a_button_are_routed_through_unchanged(string outcome)
    {
        // All three are things a rule can be configured to react to. Collapsing any of them
        // into "failed" would delete a branch an operator can see in the console.
        var executor = Build(new StubPresenter(new PromptAnswer(outcome)));
        var result = await executor.ExecuteAsync(Command(Params("ok")), null, default);

        Assert.Equal(outcome, (string?)Answer(result)["outcome"]);
    }

    [Fact]
    public async Task An_outcome_the_hub_cannot_route_is_clamped_to_failed()
    {
        // The platform half reporting something nobody asked for. Passed through it would be
        // recorded by the hub and do nothing, which reads in the history as a person who
        // answered and a rule that ignored them.
        var executor = Build(new StubPresenter(new PromptAnswer("snoozed")));
        var result = await executor.ExecuteAsync(Command(Params("yes", "no")), null, default);

        var answer = Answer(result);
        Assert.Equal("failed", (string?)answer["outcome"]);
        Assert.Contains("snoozed", (string?)answer["error"]);
    }

    [Fact]
    public async Task Nobody_to_show_it_to_is_a_successful_command_carrying_no_session()
    {
        var presenter = new StubPresenter(new PromptAnswer("ok"))
        {
            CanReachAnybody = false,
            UnreachableReason = "notifications are switched off for the FleetHub agent",
        };
        var result = await Build(presenter).ExecuteAsync(Command(Params("ok")), null, default);

        // Success, because the command ran and the answer is "there was nobody to ask".
        Assert.True(result.Success);
        var answer = Answer(result);
        Assert.Equal("no_session", (string?)answer["outcome"]);
        // And the reason travels with it: "no_session" alone sends an operator looking for a
        // signed-out user on a device that does not have the concept.
        Assert.Contains("switched off", (string?)answer["error"]);
        Assert.Null(presenter.Shown);
    }

    [Fact]
    public async Task More_buttons_than_the_device_can_draw_is_refused_rather_than_truncated()
    {
        var presenter = new StubPresenter(new PromptAnswer("ok"));
        var result = await Build(presenter)
            .ExecuteAsync(Command(Params("yes", "no", "later", "cancel")), null, default);

        var answer = Answer(result);
        Assert.Equal("failed", (string?)answer["outcome"]);
        // Names the limit, because the operator is the one who has to send a shorter message.
        Assert.Contains("at most 3", (string?)answer["error"]);
        // And nothing was shown: a truncated message would offer a choice whose fourth branch
        // the hub is holding and nobody can reach.
        Assert.Null(presenter.Shown);
    }

    [Fact]
    public async Task A_message_with_no_body_fails_the_command_instead_of_answering_it()
    {
        // The one path that reports failure. A malformed command is not a person declining,
        // and answering it would have the rule route an outcome as though somebody had been
        // asked something.
        var parameters = Params("ok");
        parameters["body"] = "   ";
        var result = await Build(new StubPresenter(new PromptAnswer("ok")))
            .ExecuteAsync(Command(parameters), null, default);

        Assert.False(result.Success);
        Assert.Contains("bad show_message parameters", result.Output);
    }

    [Fact]
    public async Task A_presenter_that_throws_still_answers()
    {
        // A command with no result shows in the console as running forever, and an operator's
        // only recourse is to wait out a timeout that does not exist.
        var result = await Build(new ThrowingPresenter())
            .ExecuteAsync(Command(Params("ok")), null, default);

        Assert.True(result.Success);
        var answer = Answer(result);
        Assert.Equal("failed", (string?)answer["outcome"]);
        Assert.Contains("exploded", (string?)answer["error"]);
    }

    [Fact]
    public void The_default_button_is_moved_to_the_front()
    {
        // A notification has no concept of a default button, and the platform draws actions in
        // order. Putting it first is the closest honest rendering; dropping it would silently
        // lose a choice the operator made in the console.
        var parameters = Params("yes", "no", "later");
        parameters["default_button"] = "later";

        var message = ShowMessageExecutor.Parse(parameters);
        Assert.Equal(new[] { "later", "yes", "no" }, message.Buttons.Select(b => b.Id).ToArray());
    }

    [Fact]
    public void A_command_carrying_no_buttons_still_gets_an_acknowledge_button()
    {
        // rules.py always sends at least one, so this is a hand-rolled command. Delivering it
        // with an OK beats refusing a message that is perfectly showable.
        var message = ShowMessageExecutor.Parse(new JsonObject
        {
            ["title"] = "Notice",
            ["body"] = "Something happened.",
        });

        Assert.Equal("ok", Assert.Single(message.Buttons).Id);
    }

    [Theory]
    // Below rules.py's MIN_MESSAGE_TIMEOUT, above its MAX, and absent.
    [InlineData(5, ShowMessageExecutor.MinTimeoutSeconds)]
    [InlineData(999_999, ShowMessageExecutor.MaxTimeoutSeconds)]
    [InlineData(0, ShowMessageExecutor.DefaultTimeoutSeconds)]
    [InlineData(600, 600)]
    public void The_wait_is_bounded_whatever_the_hub_asked_for(int asked, int expected)
    {
        var parameters = Params("ok");
        if (asked > 0) parameters["timeout_seconds"] = asked;

        var message = ShowMessageExecutor.Parse(parameters);
        Assert.Equal(expected, (int)message.Wait.TotalSeconds);
    }

    [Fact]
    public void The_wait_is_never_infinite_even_when_the_hub_asks_for_it()
    {
        // The Windows executor treats 0 as "wait indefinitely" and bounds it by the command's
        // TTL. That does not port: a slot held forever on a device with four of them is a
        // phone that shows online and accepts nothing.
        var message = ShowMessageExecutor.Parse(Params("ok"));
        Assert.True(message.Wait > TimeSpan.Zero);
        Assert.True(message.Wait <= TimeSpan.FromSeconds(ShowMessageExecutor.MaxTimeoutSeconds));
    }

    [Fact]
    public void The_non_button_outcomes_are_exactly_the_hubs()
    {
        // rules.py's NON_BUTTON_OUTCOMES, written out. A fourth copy of this list (the hub,
        // the Windows agent, this agent) is a list that drifts, and the symptom of drift is an
        // outcome the hub records and ignores.
        Assert.Equal(new[] { "timeout", "dismissed", "no_session", "failed" },
                     MessageOutcomes.NonButton.ToArray());
    }

    [Fact]
    public async Task The_answer_is_json_the_hub_can_parse_on_a_trimmed_build()
    {
        // JsonObject rather than JsonSerializer, for the reason JsonTrimmingTests exists: a
        // fully trimmed APK ships IsReflectionEnabledByDefault=false, and a serializer call
        // without generated type info throws on the device while passing here. This test runs
        // with reflection off (see the csproj), so building the answer the wrong way fails.
        var result = await Build(new StubPresenter(new PromptAnswer("yes")))
            .ExecuteAsync(Command(Params("yes", "no")), null, default);

        using var document = JsonDocument.Parse(result.Output!);
        Assert.Equal("yes", document.RootElement.GetProperty("outcome").GetString());
    }
}
