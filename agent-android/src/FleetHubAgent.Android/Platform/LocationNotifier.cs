using Android.App;
using Android.Content;
using Microsoft.Extensions.Logging;
using FleetHubAgent.Fleet.Executors;

namespace FleetHubAgent.Android.Platform;

/// <summary>
/// Tells the person holding the device that somebody asked where it is -- roadmap #23 phase B.
///
/// **This is the feature's promise, not a nicety.** An agent that can locate a device silently
/// is a tracker; one that says who asked, every time, is a management tool. The person holding
/// the device can always see they were located, the console's audit trail records the same name,
/// and the two agree because both come from the command's `issued_by`, which the hub sets from
/// the operator's session and never from anything a client sent.
///
/// **Its own notification channel, at Default importance**, unlike the agent's permanent status
/// notification which is Low and silent. That one exists because the platform requires a
/// foreground service to have one and nobody should ever look at it; this one exists to be
/// noticed. Sharing a channel would let somebody silence the disclosure by silencing the
/// service's own noise, which is exactly the wrong thing to make easy.
///
/// **Nothing here throws.** From Android 13 a notification needs a runtime permission, and a
/// device where that was refused must still answer a locate -- otherwise refusing the
/// notification would become a way to refuse being found, which inverts what the disclosure is
/// for. A failure is logged and the locate continues; see LocateDeviceExecutor, which posts
/// this before taking the fix and does not wait on it.
/// </summary>
public sealed class LocationNotifier(Context context, ILogger log) : ILocationDisclosure
{
    private const string ChannelId = "fleethub.agent.location";

    /// <summary>A fixed id, so a second locate REPLACES the first notice rather than stacking
    /// one per request. Somebody located six times in a minute should see that it happened, not
    /// have their shade filled -- and the timestamp on the single notice already says when.
    /// Distinct from AgentService's NotificationId, which must never be reused here: posting
    /// over it would replace the foreground service's own notification and the system would
    /// stop the service.</summary>
    private const int NotificationId = 2;

    public void NotifyLocated(string? requestedBy)
    {
        try
        {
            if (context.GetSystemService(Context.NotificationService)
                is not NotificationManager manager) return;

            EnsureChannel(manager);

            var who = string.IsNullOrWhiteSpace(requestedBy) ? "your organisation" : requestedBy;
            var notification = new Notification.Builder(context, ChannelId)
                .SetContentTitle("This device was located")
                .SetContentText($"{who} asked where this device is.")
                // The long form, because the short one is elided on a narrow screen and the
                // operator's address is the part that matters -- it is what makes this a
                // disclosure rather than a rumour.
                .SetStyle(new Notification.BigTextStyle().BigText(
                    $"{who} requested this device's location through FleetHub. " +
                    $"Its position was sent to your organisation's console."))
                .SetSmallIcon(global::Android.Resource.Drawable.IcMenuMyLocation)
                .SetAutoCancel(true)
                .SetWhen(Java.Lang.JavaSystem.CurrentTimeMillis())
                .SetShowWhen(true)
                .Build();

            manager.Notify(NotificationId, notification);
        }
        catch (Exception e)
        {
            // See the class docstring: a device that cannot show the notice must still answer.
            log.LogWarning("Could not post the location disclosure: {Msg}", e.Message);
        }
    }

    private static void EnsureChannel(NotificationManager manager)
    {
        // No API-26 guard: SupportedOSPlatformVersion is 26, so channels always exist here.
        var channel = new NotificationChannel(
            ChannelId, "Location requests", NotificationImportance.Default)
        {
            Description = "Shown when somebody asks this device where it is.",
        };
        channel.SetShowBadge(true);
        manager.CreateNotificationChannel(channel);
    }
}
