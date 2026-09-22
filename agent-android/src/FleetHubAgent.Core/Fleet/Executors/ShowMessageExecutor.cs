using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging;

namespace FleetHubAgent.Fleet.Executors;

/// <summary>
/// A message to the person holding the device, and which button they pressed -- roadmap #23.
///
/// **The one command where a phone is BETTER than a PC.** Every other entry in this agent's
/// command set is a smaller version of what the Windows agent does; this one is the opposite.
/// A notice on a desktop reaches somebody who happens to be at their desk. A notice on the
/// phone in their pocket reaches them in a corridor, and "we are rebooting the file server in
/// ten minutes -- OK / Later" is answered rather than found the next morning.
///
/// **It is the full contract or it is nothing.** The hub does not treat this as a fire-and-
/// forget notification: rules.py records the command id, waits for the answer, and routes the
/// outcome onto follow-up actions (handle_message_result). An agent that posted a notice and
/// reported success would leave every rule holding a message that is never answered, and the
/// symptom is a rule that silently never reaches its second half. So this reports one of the
/// hub's own outcome strings, every time, and the button ids it was given rather than any
/// meaning it invented for them.
///
/// **The agent never interprets a button**, exactly as the Windows executor does not. Ids and
/// labels come down, an id goes back, and what "Later" MEANS lives in the hub -- so changing
/// it does not require building, signing and rolling an APK out to a fleet of phones.
///
/// **"Nobody could be shown it" is a SUCCESS.** The Windows agent reports `no_session` when
/// there is no signed-in desktop; the same line is drawn here, for the Android version of the
/// same fact -- notifications switched off for this app, which leaves no way to put anything
/// in front of anybody. A failure would be indistinguishable in the console from a network
/// problem, and it would strand the hub's `no_session` route, which is how an operator says
/// "then ask again in half an hour". LocateDeviceExecutor draws the same line for the same
/// reason.
///
/// **The surface is a notification with its buttons as notification actions**, and that is a
/// decision rather than a shortcut. *Rejected: an Activity showing a real dialog.* Android 10
/// and later refuse to let a background app start an activity at all; the sanctioned way
/// around it is a full-screen-intent notification, and from Android 14 that intent needs a
/// permission granted only to calling and alarm apps. So the dialog route buys a worse
/// failure mode -- a message that shows on some devices and silently does not on others --
/// in exchange for looking more like Windows. A heads-up notification with two buttons is
/// also simply what a phone user expects an answerable message to look like.
///
/// What that surface costs is the button count, which is why <see cref="IUserMessagePresenter
/// .MaxButtons"/> exists; see <see cref="TooManyButtons"/>.
/// </summary>
public sealed class ShowMessageExecutor(
    ILogger<ShowMessageExecutor> log,
    IUserMessagePresenter presenter) : ICommandExecutor
{
    /// <summary>Matches hub/fleet.py's USER_MESSAGE_COMMANDS and the Windows executor's
    /// Type. A mismatch routes the command nowhere and answers "unsupported" to a console
    /// that is showing the button.</summary>
    public string Type => "show_message";

    /// <summary>Used when the hub sends no timeout at all.
    ///
    /// **Not "wait forever", which is what the Windows executor does with the same input.**
    /// A waiting message holds one of AgentConfig.MaxConcurrentCommands slots for as long as
    /// it waits, so an unanswered indefinite message on a phone is a slot that never comes
    /// back -- and a device that quietly stops accepting commands is the failure this whole
    /// agent is most careful about elsewhere. An hour is long enough that a person who walks
    /// away from their desk still answers it, and short enough that a message nobody was ever
    /// going to answer stops costing anything.</summary>
    internal const int DefaultTimeoutSeconds = 3600;

    /// <summary>Floor and ceiling on whatever the hub asked for. Both match rules.py's
    /// MIN_MESSAGE_TIMEOUT and MAX_MESSAGE_TIMEOUT, so a message behaves the same whether the
    /// hub bounded it or a hand-rolled command did not.</summary>
    internal const int MinTimeoutSeconds = 30;
    internal const int MaxTimeoutSeconds = 12 * 3600;

    public async Task<CommandResult> ExecuteAsync(
        FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        UserMessage message;
        try
        {
            message = Parse(cmd.Params);
        }
        catch (Exception e)
        {
            // The one path that FAILS the command rather than answering it. A message with no
            // body is not a person declining to answer, it is a malformed command, and the
            // difference matters to the rule holding the other end: an outcome would be
            // routed as though somebody had been asked something.
            return CommandResult.Fail($"bad show_message parameters: {e.Message}");
        }

        var shownAt = DateTimeOffset.UtcNow.ToUnixTimeSeconds();

        if (message.Buttons.Count > presenter.MaxButtons)
        {
            // **Refused rather than truncated**, and that is the point of the check. Android
            // renders at most three notification actions; a fourth button would be added,
            // never drawn, and its route in the hub would be one nobody could ever reach --
            // a rule branch that looks configured and can only time out. Reported as `failed`
            // because that outcome is routable and lands in the console with the reason
            // attached, where a command-level failure would read as a broken agent.
            log.LogWarning("show_message: {Count} buttons, this device shows {Max}",
                message.Buttons.Count, presenter.MaxButtons);
            return Answer(MessageOutcomes.Failed, shownAt,
                          TooManyButtons(message.Buttons.Count, presenter.MaxButtons));
        }

        if (!presenter.CanReachAnybody)
        {
            log.LogInformation("show_message: nothing can be shown on this device, no_session");
            return Answer(MessageOutcomes.NoSession, shownAt, presenter.UnreachableReason);
        }

        PromptAnswer answer;
        try
        {
            answer = await presenter.AskAsync(message, ct);
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            // The agent is stopping, not a person declining. Answered rather than thrown, so
            // the hub records something instead of showing the command as running forever.
            log.LogInformation("show_message: the agent was stopping");
            return Answer(MessageOutcomes.Failed, shownAt, "the agent was stopping");
        }
        catch (Exception e)
        {
            log.LogWarning(e, "show_message: the presenter threw");
            return Answer(MessageOutcomes.Failed, shownAt, e.Message);
        }

        var outcome = Routable(answer.Outcome, message)
            ? answer.Outcome
            : MessageOutcomes.Failed;
        var error = answer.Error;
        if (outcome != answer.Outcome)
        {
            // The platform half reported something the hub has no route for. Clamped here
            // rather than passed through, because rules.py records an unmapped outcome and
            // does nothing -- so an invented string would show in the history as a person who
            // answered and a rule that ignored them.
            log.LogWarning("show_message: unroutable outcome {Outcome}", answer.Outcome);
            error = $"the device reported '{answer.Outcome}', which is not one of this " +
                    $"message's outcomes";
        }

        log.LogInformation("show_message: outcome {Outcome}", outcome);
        return Answer(outcome, shownAt, error);
    }

    /// <summary>The refusal text for a message with more buttons than this device can draw.
    /// Internal so a test can hold it against <see cref="IUserMessagePresenter.MaxButtons"/>
    /// -- an operator reading it has to learn what to change, and "failed" on its own sends
    /// them looking at the phone.</summary>
    internal static string TooManyButtons(int asked, int max) =>
        $"this message has {asked} buttons and an Android notification shows at most {max}. " +
        $"Send it with {max} or fewer, or the person holding the device would be offered a " +
        $"choice they cannot make.";

    /// <summary>Whether the hub can route this outcome. Every button id, plus the four
    /// outcomes that are not a button press -- rules.py's NON_BUTTON_OUTCOMES.</summary>
    private static bool Routable(string outcome, UserMessage message) =>
        MessageOutcomes.NonButton.Contains(outcome, StringComparer.Ordinal)
        || message.Buttons.Any(b => string.Equals(b.Id, outcome, StringComparison.Ordinal));

    /// <summary>The wire shape rules.handle_message_result reads. Keys are the hub's, and the
    /// only one it actually routes on is `outcome` -- the timestamps are for the operator
    /// reading the command's result, which is why a message answered in four hours still says
    /// so rather than looking instant.</summary>
    internal static CommandResult Answer(string outcome, long shownAt, string? error = null)
    {
        var json = new JsonObject
        {
            ["outcome"] = outcome,
            ["shown_at"] = shownAt,
            ["responded_at"] = DateTimeOffset.UtcNow.ToUnixTimeSeconds(),
        };
        if (!string.IsNullOrWhiteSpace(error)) json["error"] = error;
        // Success even for `failed`: the COMMAND ran. What failed is the asking, and that
        // distinction is what lets an operator tell a device that could not show a message
        // from one that never received the command.
        return CommandResult.Ok(json.ToJsonString());
    }

    /// <summary>Read the hub's params. Throws only for a message that could not be shown to
    /// anybody whatever the device -- no title, no body, no buttons.</summary>
    internal static UserMessage Parse(JsonNode? parameters)
    {
        var title = (parameters.GetString("title") ?? "").Trim();
        var body = (parameters.GetString("body") ?? "").Trim();
        if (title.Length == 0) throw new ArgumentException("a message needs a title");
        if (body.Length == 0) throw new ArgumentException("a message needs a body");

        var buttons = ParseButtons(parameters);
        if (buttons.Count == 0) throw new ArgumentException("a message needs at least one button");

        var asked = parameters.GetInt("timeout_seconds", 0);
        var timeout = asked <= 0
            ? DefaultTimeoutSeconds
            : Math.Clamp(asked, MinTimeoutSeconds, MaxTimeoutSeconds);

        // `style` and `target_session` are read and dropped, deliberately. A Windows session
        // id means nothing on a device with one user, and `toast` versus `dialog` is a choice
        // between two window shapes this platform does not have -- the Windows agent ignores
        // it too. Answering a message quietly would be the worse reading of `toast`: the hub
        // is waiting for an answer either way, so a notice nobody notices becomes a timeout.
        return new UserMessage(title, body, buttons, TimeSpan.FromSeconds(timeout));
    }

    private static List<MessageButton> ParseButtons(JsonNode? parameters)
    {
        var buttons = new List<MessageButton>();
        if (parameters is not JsonObject obj
            || !obj.TryGetPropertyValue("buttons", out var node)
            || node is not JsonArray array)
        {
            // No list at all. The hub always sends one (rules.py defaults to a single OK), so
            // this is a hand-rolled command, and one acknowledge button is the reading that
            // still delivers the message rather than refusing it.
            return [new MessageButton(MessageOutcomes.Ok, null)];
        }

        foreach (var entry in array)
        {
            var id = entry is JsonValue value ? value.ToString() : entry.GetString("id");
            if (string.IsNullOrWhiteSpace(id)) continue;
            var label = entry.GetString("label");
            buttons.Add(new MessageButton(id.Trim(),
                string.IsNullOrWhiteSpace(label) ? null : label.Trim()));
        }

        // **The default button goes first**, because a notification has no concept of one.
        // It is the closest honest rendering: the platform draws actions left to right and
        // the first is the one a thumb reaches, so the hub's preference survives as an order
        // rather than being silently dropped.
        var preferred = parameters.GetString("default_button");
        if (!string.IsNullOrWhiteSpace(preferred))
        {
            var index = buttons.FindIndex(b => string.Equals(b.Id, preferred, StringComparison.Ordinal));
            if (index > 0)
            {
                var button = buttons[index];
                buttons.RemoveAt(index);
                buttons.Insert(0, button);
            }
        }
        return buttons;
    }
}

/// <summary>One message, in the shape the platform half has to render.
///
/// Deliberately not the hub's params object: everything the hub sends that this platform
/// cannot honour is dropped by <see cref="ShowMessageExecutor.Parse"/>, so the Android half
/// cannot accidentally decide what to do about a Windows session id.</summary>
/// <param name="Title">Shown as the notification's title.</param>
/// <param name="Body">The message itself.</param>
/// <param name="Buttons">At least one, default first.</param>
/// <param name="Wait">How long to leave it up before reporting a timeout. Always bounded --
/// see ShowMessageExecutor.DefaultTimeoutSeconds for why this is never infinite here.</param>
public sealed record UserMessage(
    string Title,
    string Body,
    IReadOnlyList<MessageButton> Buttons,
    TimeSpan Wait);

/// <summary>One button. <paramref name="Label"/> is null when the hub sent no words for it,
/// which is the ordinary case -- the presets carry ids only, and the words are the platform's
/// to supply.</summary>
public sealed record MessageButton(string Id, string? Label);

/// <summary>What the person did, as the platform saw it.</summary>
/// <param name="Outcome">A button id, or one of <see cref="MessageOutcomes.NonButton"/>.</param>
/// <param name="Error">Why, when the outcome is <c>failed</c>. Reaches the operator's command
/// result, so it is phrased for them rather than for a developer.</param>
public sealed record PromptAnswer(string Outcome, string? Error = null);

/// <summary>Putting a question in front of whoever is holding this device, and waiting.
///
/// **An interface rather than a call into the notification manager**, for the same reason
/// ILocationSource is one: everything about what an answer MEANS is the executor's business,
/// in Core, where a workstation can test it. The platform half only has to put buttons on a
/// screen and say which one was pressed.</summary>
public interface IUserMessagePresenter
{
    /// <summary>False when nothing this agent posts can be seen -- notifications switched off
    /// for the app, or its message channel blocked. Answers the hub's `no_session` case.
    /// </summary>
    bool CanReachAnybody { get; }

    /// <summary>Why not, phrased for the operator reading the command result. "Notifications
    /// are switched off for the agent on this device" and "nobody is signed in" lead to
    /// completely different next steps, and a bare `no_session` says neither.</summary>
    string? UnreachableReason { get; }

    /// <summary>How many buttons this device can actually draw. Declared by the platform and
    /// enforced in Core, so the limit is testable and the rule about what to do when it is
    /// exceeded is written once.</summary>
    int MaxButtons { get; }

    /// <summary>Show it and wait. Must return a <see cref="PromptAnswer"/> rather than throw
    /// for every ordinary end -- a press, a dismissal, or the wait running out.</summary>
    Task<PromptAnswer> AskAsync(UserMessage message, CancellationToken ct);
}

/// <summary>The outcomes that are not a button press, and the one button id this agent names
/// itself.
///
/// Values rather than an enum because they cross a JSON boundary into Python: rules.py's
/// routing table is keyed on exactly these strings (NON_BUTTON_OUTCOMES), and an outcome it
/// cannot route is recorded and silently does nothing. Keep the two lists in step; the
/// Windows agent's MessageOutcomes is the third copy.</summary>
public static class MessageOutcomes
{
    /// <summary>rules.py's BUTTON_OK -- the id of the acknowledge button the hub falls back
    /// to, and the one this agent supplies when a hand-rolled command sends no buttons.
    /// </summary>
    public const string Ok = "ok";

    /// <summary>The wait ran out with nobody answering.</summary>
    public const string Timeout = "timeout";

    /// <summary>The notification was swiped away -- the Android reading of the Windows
    /// agent's Esc or close box, and the same claim: somebody saw it and declined to engage.
    /// </summary>
    public const string Dismissed = "dismissed";

    /// <summary>There was no way to put it in front of anybody. On Windows that is a
    /// logged-out PC; here it is an app whose notifications are switched off.</summary>
    public const string NoSession = "no_session";

    /// <summary>It could not be shown at all.</summary>
    public const string Failed = "failed";

    public static readonly IReadOnlyList<string> NonButton = [Timeout, Dismissed, NoSession, Failed];
}
