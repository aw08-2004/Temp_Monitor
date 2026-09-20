using System.Collections.Concurrent;
using Android.App;
using Android.Content;
using Microsoft.Extensions.Logging;
using FleetHubAgent.Fleet.Executors;

namespace FleetHubAgent.Android.Platform;

/// <summary>
/// Puts an operator's message in front of whoever is holding the device, and waits for them to
/// press one of its buttons -- the Android half of ShowMessageExecutor (roadmap #23).
///
/// **A heads-up notification with its buttons as notification actions**, and the reason is in
/// ShowMessageExecutor's docstring: Android 10 and later refuse to let a background app start
/// an activity, so a real dialog would have to ride a full-screen intent, whose permission
/// Android 14 restricts to calling and alarm apps. A message that shows on some phones and
/// silently does not on others is a worse feature than one that looks like every other thing
/// competing for that person's attention.
///
/// **Its own channel at High importance.** The agent's permanent status notification is Low
/// and silent because nobody should ever look at it; this one exists to interrupt, and it
/// carries a question somebody is waiting on an answer to. Sharing a channel would let
/// silencing the service's noise also silence every message an operator ever sends, and the
/// console would show a fleet that never answers.
///
/// **NOT `setOngoing`.** An ongoing notification cannot be swiped away, which sounds like the
/// right call for something that needs answering and is not: the hub has a `dismissed` outcome
/// and rules route on it, so making dismissal impossible would delete a branch operators can
/// configure and leave those messages to time out an hour later instead. Someone declining to
/// engage is an answer.
///
/// **The wait does not survive the process.** A message outstanding when Android kills this
/// process is never answered, and the hub shows the command as running until its TTL expires.
/// That is true of every in-flight command on this agent and is not worth a persistence layer
/// for: the rule that sent it gets its answer from the next fire, and a message re-posted from
/// disk hours after its moment had passed would be worse than none.
/// </summary>
public sealed class UserMessageNotifier(Context context, ILogger log) : IUserMessagePresenter
{
    private const string ChannelId = "fleethub.agent.message";

    /// <summary>Distinct from AgentService's 1 and LocationNotifier's 2, and it must stay
    /// that way: posting over AgentService's id would replace the foreground service's own
    /// notification, and the system then stops the service.
    ///
    /// **The id is fixed and the TAG is what makes each message its own notification**, since
    /// the platform identifies one by the (tag, id) pair. An earlier version of this file used
    /// the bare id with no tag and argued that a second message replacing the first was "the
    /// honest rendering of a device that answers one at a time". That was wrong the moment
    /// AgentConfig.MaxConcurrentCommands went to 4 in the same change -- and the damage was
    /// not the replacing, it was the cleanup. Two messages outstanding, A's wait ends first,
    /// A's finally cancels the id, and the notification it takes down is B's. B is then
    /// waiting on a banner nobody can see, and `cancel` does not fire a delete intent, so B is
    /// not even reported `dismissed`: it sits until its own timeout, and the rule behind it
    /// looks configured and never fires. That is precisely the failure this whole feature is
    /// written to avoid, so each message posts and cancels under its own prompt id.</summary>
    private const int NotificationId = 3;

    /// <summary>The messages currently on screen, keyed by the id their buttons carry.
    ///
    /// **Static because the platform constructs the receiver, not this class.** Android
    /// instantiates a BroadcastReceiver itself when a PendingIntent fires, so there is no
    /// instance for it to call back into and no way to hand it one. A static table is the only
    /// handoff available; it is keyed by a per-message id rather than holding "the" pending
    /// message, so two messages racing cannot answer each other.</summary>
    private static readonly ConcurrentDictionary<string, TaskCompletionSource<string>> Waiting = new();

    /// <summary>**Every button needs its own request code, and this is why.** Two PendingIntents
    /// are the same object to the platform when their action, data and component match --
    /// extras are NOT compared. Built with one request code, every button on a message would
    /// hand back whichever outcome was registered first, so a fleet-wide "Yes / No" would
    /// record every answer as Yes, silently, with nothing in any log. Incremented per intent
    /// rather than per message, so two messages outstanding at once cannot collide either.
    /// </summary>
    private static int _requestCode;

    public int MaxButtons => 3;

    public bool CanReachAnybody => Reason() is null;

    public string? UnreachableReason => Reason();

    /// <summary>Why nothing can be shown, or null when something can.
    ///
    /// Two separate switches, because they are in two different places on the phone and an
    /// operator who is going to ask somebody to change one has to be told which. Notifications
    /// off for the app is the app's own toggle; a channel at Importance.None is this one
    /// message channel muted while the rest of the agent still speaks.</summary>
    private string? Reason()
    {
        try
        {
            if (context.GetSystemService(Context.NotificationService) is not NotificationManager m)
                return "this device has no notification manager";
            if (!m.AreNotificationsEnabled())
                return "notifications are switched off for the FleetHub agent on this device, " +
                       "so nothing can be shown to the person holding it";
            var channel = m.GetNotificationChannel(ChannelId);
            if (channel is not null && channel.Importance == NotificationImportance.None)
                return "the agent's Messages notification channel is blocked on this device";
            return null;
        }
        catch (Exception e)
        {
            // Never throws: the executor turns a null reason into "go ahead and post it", and
            // a device that cannot answer this question is better off trying than being told
            // no_session on the strength of an exception reading a setting.
            log.LogWarning("Could not read notification settings: {Msg}", e.Message);
            return null;
        }
    }

    public async Task<PromptAnswer> AskAsync(UserMessage message, CancellationToken ct)
    {
        if (context.GetSystemService(Context.NotificationService) is not NotificationManager manager)
            return new PromptAnswer(MessageOutcomes.Failed, "this device has no notification manager");

        var promptId = Guid.NewGuid().ToString("N");
        var answered = new TaskCompletionSource<string>(TaskCreationOptions.RunContinuationsAsynchronously);
        Waiting[promptId] = answered;

        try
        {
            EnsureChannel(manager);
            manager.Notify(promptId, NotificationId, Build(message, promptId));
            log.LogInformation("Message posted, {Count} buttons, waiting {Seconds}s",
                message.Buttons.Count, (int)message.Wait.TotalSeconds);

            // Linked so that the agent stopping ends the wait as well as the clock running
            // out. Cancelled on the way out either way, so a twelve-hour message that was
            // answered in ten seconds does not leave a twelve-hour timer behind it.
            using var clock = CancellationTokenSource.CreateLinkedTokenSource(ct);
            var expiry = Task.Delay(message.Wait, clock.Token);
            var finished = await Task.WhenAny(answered.Task, expiry);
            clock.Cancel();

            if (ReferenceEquals(finished, answered.Task))
                return new PromptAnswer(await answered.Task);

            // The delay ended. Either the agent is stopping -- which the executor reports as
            // `failed` with its own reason -- or nobody answered in time.
            ct.ThrowIfCancellationRequested();
            log.LogInformation("Message timed out unanswered");
            return new PromptAnswer(MessageOutcomes.Timeout);
        }
        finally
        {
            Waiting.TryRemove(promptId, out _);
            // Taken down however this ended, and **by tag, so only this message's own banner
            // goes** -- see NotificationId. A timed-out message left on screen is a button
            // somebody presses tomorrow, into a command the hub finished with hours ago.
            try { manager.Cancel(promptId, NotificationId); } catch { /* best effort */ }
        }
    }

    /// <summary>Record an answer. Called from <see cref="MessageResponseReceiver"/>, on the
    /// platform's thread, possibly for a message this process has already given up on -- hence
    /// TryRemove and TrySetResult rather than anything that would throw on a stale press.
    /// </summary>
    internal static void Answered(string promptId, string outcome)
    {
        if (Waiting.TryRemove(promptId, out var waiting)) waiting.TrySetResult(outcome);
    }

    private Notification Build(UserMessage message, string promptId)
    {
        var builder = new Notification.Builder(context, ChannelId)
            .SetContentTitle(message.Title)
            .SetContentText(message.Body)
            // The long form as well as the short one: the short is elided after a line on a
            // narrow screen, and a notice somebody is being asked to agree to is exactly the
            // kind that must be readable in full before they press anything.
            .SetStyle(new Notification.BigTextStyle().BigText(message.Body))
            .SetSmallIcon(global::Android.Resource.Drawable.IcDialogInfo)
            .SetAutoCancel(false)
            .SetWhen(Java.Lang.JavaSystem.CurrentTimeMillis())
            .SetShowWhen(true)
            // Swiping it away is an answer -- see the class docstring.
            .SetDeleteIntent(Respond(promptId, MessageOutcomes.Dismissed));

        foreach (var button in message.Buttons)
        {
            var action = new Notification.Action.Builder(
                global::Android.Resource.Drawable.IcMenuInfoDetails,
                Label(button), Respond(promptId, button.Id)).Build();
            builder.AddAction(action);
        }

        return builder.Build();
    }

    /// <summary>The words on a button.
    ///
    /// **The hub's label wins when it sent one**, because an operator who wrote "Reboot now"
    /// meant those words. The fallbacks below cover the ordinary case, where rules.py sends a
    /// preset carrying ids and no labels at all -- deliberately, so that what a button says
    /// stays translatable at one end rather than being baked into an APK.
    ///
    /// English, like every other string this agent puts on a device (see LocationNotifier).
    /// The hub's three catalogs cover the console; the agent has never had an i18n layer, and
    /// adding one for seven words would be a second place for them to disagree. An unknown id
    /// falls through to itself rather than to "OK", so a button the hub invents later is
    /// pressable rather than mislabelled.</summary>
    private static string Label(MessageButton button) => button.Label ?? button.Id switch
    {
        "ok" => "OK",
        "cancel" => "Cancel",
        "yes" => "Yes",
        "no" => "No",
        "later" => "Later",
        "accept" => "Accept",
        "decline" => "Decline",
        var other => other,
    };

    private PendingIntent Respond(string promptId, string outcome)
    {
        var intent = new Intent(context, typeof(MessageResponseReceiver))
            .SetAction(MessageResponseReceiver.ActionAnswer)
            .PutExtra(MessageResponseReceiver.ExtraPrompt, promptId)
            .PutExtra(MessageResponseReceiver.ExtraOutcome, outcome);

        // Immutable from API 31, where the platform requires one of the two to be stated. The
        // receiver takes everything it needs from the extras above, so nothing downstream has
        // any business changing them.
        var flags = PendingIntentFlags.UpdateCurrent
                    | (OperatingSystem.IsAndroidVersionAtLeast(31)
                        ? PendingIntentFlags.Immutable : 0);
        return PendingIntent.GetBroadcast(
            context, Interlocked.Increment(ref _requestCode), intent, flags)!;
    }

    private static void EnsureChannel(NotificationManager manager)
    {
        // No API-26 guard: SupportedOSPlatformVersion is 26, so channels always exist here.
        // High rather than Default, which is what makes it a heads-up banner rather than a
        // line in the shade nobody opens until the evening.
        var channel = new NotificationChannel(
            ChannelId, "Messages", NotificationImportance.High)
        {
            Description = "Notices from your IT team that ask you to answer.",
        };
        channel.SetShowBadge(true);
        manager.CreateNotificationChannel(channel);
    }
}

/// <summary>
/// Carries a pressed button back to the message that is waiting for it.
///
/// **Exported = false, and unlike BootReceiver that is not a judgement call.** BootReceiver
/// must be exported because the system sends it a boot broadcast; nothing outside this app has
/// any reason to answer a question put to this device's holder, and an exported one would let
/// any app on the phone report that somebody agreed to a reboot. It is addressed by explicit
/// intent from the same process, which needs no intent filter and no export.
///
/// It does the least it possibly can -- read two extras, hand them to the table, return --
/// because a receiver gets about ten seconds before the platform considers it hung, and the
/// work that follows an answer belongs to the command loop that is already waiting on it.
/// </summary>
[BroadcastReceiver(Enabled = true, Exported = false)]
public sealed class MessageResponseReceiver : BroadcastReceiver
{
    internal const string ActionAnswer = "net.arkeanos.fleethub.agent.MESSAGE_ANSWER";
    internal const string ExtraPrompt = "prompt";
    internal const string ExtraOutcome = "outcome";

    public override void OnReceive(Context? context, Intent? intent)
    {
        if (intent?.Action != ActionAnswer) return;

        var promptId = intent.GetStringExtra(ExtraPrompt);
        var outcome = intent.GetStringExtra(ExtraOutcome);
        if (string.IsNullOrEmpty(promptId) || string.IsNullOrEmpty(outcome)) return;

        global::Android.Util.Log.Info("FleetHubAgent", $"Message answered: {outcome}");
        UserMessageNotifier.Answered(promptId, outcome);
    }
}
