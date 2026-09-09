using Android.App;
using Android.Content;
using Android.Content.PM;
using Microsoft.Extensions.Logging;
using FleetHubAgent.Update;
using FleetHubAgent.Android.Policy;

namespace FleetHubAgent.Android.Platform;

/// <summary>
/// Installing this app's own next version -- roadmap #22.
///
/// The platform half of <see cref="IPackageInstaller"/>, and deliberately the whole of it:
/// every decision about whether to install, what to trust and when to give up is in
/// <see cref="SelfUpdater"/>. This file knows how to open a session, write bytes into it, and
/// commit.
///
/// **Silent install is a Device Owner power, and there is no fallback.** Without device
/// ownership `commit()` raises the system's package-installer UI, which on a phone in a drawer
/// means an update that waits forever behind a dialog nobody will ever tap -- and on a phone
/// somebody IS holding, a prompt they did not ask for and have no context to judge. Neither is
/// an update mechanism, so an unmanaged build simply does not self-update and says so once.
///
/// **Android checks the signature too, and that check is not this one.** A package can only
/// replace an installed one if it is signed by the same key, which the platform enforces
/// regardless of what we do. That is a different claim from the manifest signature
/// SelfUpdater verifies: Android's says "same author as what is installed", ours says "the
/// fleet's release key approved this exact build". An attacker who could publish an APK signed
/// with the fleet's Android keystore would pass Android's check and still fail ours.
///
/// **Committing does not return.** The platform kills this process as part of replacing the
/// app, so nothing after the commit runs -- which is why the agent comes back through a
/// broadcast (BootReceiver's ACTION_MY_PACKAGE_REPLACED) rather than by resuming here.
/// </summary>
public sealed class AndroidPackageInstaller(Context context, ILogger log) : IPackageInstaller
{
    /// <summary>Evaluated live rather than captured, for the same reason DeviceOwner.Features
    /// is: `adb shell dpm set-device-owner` can grant ownership to a running process.</summary>
    public bool CanInstallSilently => DeviceOwner.IsManaged(context);

    public string? Install(string path)
    {
        if (!CanInstallSilently) return "this device is not fully managed";

        PackageInstaller? installer = null;
        int sessionId = -1;
        try
        {
            installer = context.PackageManager?.PackageInstaller;
            if (installer is null) return "no package installer on this device";

            var parameters = new PackageInstaller.SessionParams(PackageInstallMode.FullInstall);
            sessionId = installer.CreateSession(parameters);
            using var session = installer.OpenSession(sessionId);

            // Streamed rather than read into memory: this is a ten-megabyte APK on a device
            // whose process is already holding a foreground service, and OpenWrite exists
            // precisely so the whole file never has to be resident twice.
            using (var source = File.OpenRead(path))
            using (var destination = session.OpenWrite("agent.apk", 0, source.Length))
            {
                source.CopyTo(destination);
                session.Fsync(destination);
            }

            // The IntentSender the platform reports the outcome to. **Nothing listens for it**,
            // and that is not an oversight: on success this process is killed before any
            // result could be delivered, and on failure SelfUpdater has already recorded the
            // attempt and will log the next check. A receiver whose only job is to observe a
            // message that arrives after the process dies would be a component that exists to
            // look thorough.
            var intent = new Intent(context, typeof(AgentService));
            var flags = PendingIntentFlags.UpdateCurrent
                        | (OperatingSystem.IsAndroidVersionAtLeast(31)
                            ? PendingIntentFlags.Mutable : 0);
            var pending = PendingIntent.GetService(context, 0, intent, flags);
            session.Commit(pending!.IntentSender!);

            // Rarely reached. The platform usually stops this process inside Commit.
            log.LogInformation("Install session {Id} committed", sessionId);
            return null;
        }
        catch (Exception e)
        {
            // Never thrown: the contract says this returns a reason, because an exception here
            // would reach SelfUpdater's outer handler and be logged as "unexpected error",
            // which reads like a transient fault rather than a refused install.
            log.LogWarning(e, "Install session failed");
            if (installer is not null && sessionId >= 0)
            {
                // Abandon it, or the session sits in the platform's list until it expires and
                // the next attempt starts beside it rather than instead of it.
                try { installer.AbandonSession(sessionId); } catch { /* best effort */ }
            }
            return e.Message;
        }
    }
}
