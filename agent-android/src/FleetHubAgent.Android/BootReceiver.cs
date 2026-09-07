using Android.App;
using Android.Content;

namespace FleetHubAgent.Android;

/// <summary>
/// Restarts the agent after the device boots.
///
/// **Without this a managed device is managed exactly once.** A phone reboots for a system
/// update, a flat battery, or a crash, and there is nobody at that device to reopen an app --
/// the machine would simply stop reporting, and in the console it would be indistinguishable
/// from a device that had been switched off or left the building.
///
/// Three broadcasts are registered rather than one (see the manifest): several OEM skins send
/// only their own QUICKBOOT_POWERON instead of BOOT_COMPLETED. Listening for just the standard
/// action works on most devices and silently fails forever on the rest, which is the worst
/// shape a bug can have here -- one model that never comes back, looking like dead hardware.
///
/// **Exported, and it must be**, because the sender is the system. That is not a hole: the
/// receiver takes no data from the Intent and does exactly one thing regardless of who sent it
/// -- start a service that was going to run anyway. The action is checked all the same, so a
/// stray broadcast does not read as a boot.
/// </summary>
[BroadcastReceiver(Enabled = true, Exported = true)]
[IntentFilter([
    Intent.ActionBootCompleted,
    "android.intent.action.QUICKBOOT_POWERON",
    "com.htc.intent.action.QUICKBOOT_POWERON",
])]
public sealed class BootReceiver : BroadcastReceiver
{
    public override void OnReceive(Context? context, Intent? intent)
    {
        if (context is null || intent?.Action is not { Length: > 0 } action) return;

        if (action is not (Intent.ActionBootCompleted
                or "android.intent.action.QUICKBOOT_POWERON"
                or "com.htc.intent.action.QUICKBOOT_POWERON"))
            return;

        global::Android.Util.Log.Info("FleetHubAgent", $"Boot ({action}); starting the agent");
        AgentService.Start(context);
    }
}
