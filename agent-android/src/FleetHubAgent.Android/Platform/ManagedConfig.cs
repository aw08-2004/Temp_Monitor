using Android.Content;
using Android.OS;
using FleetHubAgent;
using FleetHubAgent.State;

namespace FleetHubAgent.Android.Platform;

/// <summary>
/// Configuration pushed by an MDM, read through RestrictionsManager.
///
/// **This is the path that matters for a real deployment, and the setup screen is the
/// fallback.** The Windows and Linux agents are installed by a script that already holds the
/// enrollment secret, so there is nothing to type. A phone has no such script: without managed
/// configuration, enrolling fifty devices means somebody typing a shared secret into fifty
/// handsets, which is both an afternoon and a secret that ends up in a group chat. An MDM sets
/// these keys once, on a policy, and every device that receives it enrolls itself.
///
/// **Managed configuration is read live on every call and is never copied into the agent's own
/// store.** That is deliberate: an administrator who corrects the hub URL on the policy expects
/// the correction to take, and a value cached at first launch -- or worse, written into
/// SharedPreferences where it would outrank the policy -- would leave a device pointed at the
/// old hub with no way to tell from the console.
///
/// The keys are declared in Resources/xml/app_restrictions.xml, which is what makes them appear
/// as fields in the MDM's own console. A key added here and not there can still be pushed, but
/// only by an administrator who knows to type its exact name.
/// </summary>
internal static class ManagedConfig
{
    internal const string KeyHubUrl = "hub_url";
    internal const string KeyEnrollmentSecret = "enrollment_secret";
    internal const string KeyMachineName = "machine_name";
    internal const string KeyAssetTag = "asset_tag";

    private static Bundle? Restrictions(Context context)
    {
        try
        {
            if (context.ApplicationContext?.GetSystemService(Context.RestrictionsService)
                is not RestrictionsManager manager) return null;
            return manager.ApplicationRestrictions;
        }
        catch { return null; }
    }

    internal static string? Read(Context context, string key)
    {
        var value = Restrictions(context)?.GetString(key);
        return string.IsNullOrWhiteSpace(value) ? null : value.Trim();
    }

    /// <summary>The hub URL an MDM has set, or null.</summary>
    public static string? HubUrl(Context context) => Read(context, KeyHubUrl);

    /// <summary>An asset tag from the organisation's own asset register. The only honest source
    /// for that field on Android -- see AndroidSystemInfo.</summary>
    public static string? AssetTag(Context context) => Read(context, KeyAssetTag);

    /// <summary>A machine name an MDM has pinned.
    ///
    /// **Applied only when this device is still on its derived default.** An MDM pushing a name
    /// to a group of devices must not silently undo a rename an operator did from the console
    /// afterwards -- the operator would rename it, watch it change back at the next launch, and
    /// have no way to see why. First launch wins for the MDM; every launch after that respects
    /// what the fleet has been told.</summary>
    public static void ApplyMachineName(Context context, MachineNameProvider names)
    {
        if (!names.IsDefault) return;
        var pinned = Read(context, KeyMachineName);
        if (pinned is null) return;
        names.TrySet(pinned, out _);
    }

    /// <summary>Point the agent at the configured hub: the MDM's value first, then whatever an
    /// operator typed on the setup screen or arrived in the provisioning QR, then the
    /// compiled-in placeholder.
    ///
    /// **Says so, loudly, when none of them supplied anything.** The placeholder is not a hub
    /// (see AgentConfig.DefaultHubBase), so a device that reaches this point unconfigured will
    /// run perfectly and report nowhere. logcat is the only place a technician can see that
    /// before the console fails to show the device.</summary>
    public static void ApplyHubUrl(Context context, AgentState state)
    {
        AgentConfig.Configure(HubUrl(context) ?? state.LoadHubBaseOverride());

        if (AgentConfig.IsHubConfigured)
        {
            global::Android.Util.Log.Info("FleetHubAgent", $"Hub: {AgentConfig.HubBase}");
            return;
        }

        global::Android.Util.Log.Error("FleetHubAgent",
            "NO HUB CONFIGURED. This device will report nowhere. Nothing supplied a hub URL: " +
            "not managed configuration, not the provisioning QR, not the setup screen.");
    }
}

/// <summary>
/// Where the shared enrollment secret comes from on Android: managed configuration first, then
/// what an operator typed on the setup screen.
///
/// **Asked afresh on every enrollment attempt rather than cached**, which is the difference
/// from every other agent in this repo. A systemd EnvironmentFile cannot change under a running
/// process; an MDM policy can arrive minutes after the device booted, and someone can type the
/// secret into the setup screen while the service is already running. A value read once at
/// startup would leave such a device telemetry-only until it was restarted, and nobody would
/// think to restart it -- it looks online and healthy in the console, and simply never takes a
/// command.
///
/// The MDM wins over the typed value for the same reason it wins for the hub URL: an
/// administrator rotating the fleet's secret on a policy expects that to take effect, not to
/// lose to a copy somebody pasted in during setup.
/// </summary>
internal sealed class AndroidEnrollmentSecretSource(Context context, AgentState state) : IEnrollmentSecretSource
{
    public string? Read()
    {
        try
        {
            return ManagedConfig.Read(context, ManagedConfig.KeyEnrollmentSecret)
                   ?? state.LoadEnrollmentSecret();
        }
        catch { return null; }
    }
}

/// <summary>
/// Configuration carried INSIDE the provisioning QR, adopted once when a device is provisioned.
///
/// **This is the source that was missing, and its absence made QR provisioning useless.** The
/// hub mints a QR whose ADMIN_EXTRAS_BUNDLE carries the hub URL and the enrollment secret (see
/// hub/provisioning.py's BUNDLE_HUB_URL and BUNDLE_ENROLLMENT_SECRET), which is the entire point
/// of provisioning by QR: nobody types a fleet-wide credential into a handset. The agent read
/// only <see cref="ManagedConfig"/>, which is RestrictionsManager -- and application
/// restrictions are set by an EXTERNAL device policy controller. On a device where this app IS
/// the controller nothing sets them, so a QR-provisioned device found no hub URL and no secret,
/// silently kept the compiled-in <see cref="AgentConfig.DefaultHubBase"/>, and reported nowhere.
/// It looked identical, from the device, to a working install: device owner confirmed, service
/// running, notification posted, and nothing in the console. **Observed on the first
/// QR-provisioned device.**
///
/// **The bundle is written into the agent's own store, not held in memory.** It arrives exactly
/// once, in the intent that completes provisioning, and a value only in memory would be gone the
/// first time the platform killed the process -- leaving a device that enrolled once and could
/// never enroll again. Writing it also puts it at the right precedence: below live managed
/// configuration, which an administrator can still correct on a policy, and above the
/// compiled-in default. See <see cref="AndroidEnrollmentSecretSource"/> for the same ordering.
///
/// Only the two keys the hub actually sends are read. A key the hub does not mint has never been
/// in a QR, so handling it here would be code that cannot be exercised.
/// </summary>
internal static class ProvisioningExtras
{
    // android.app.admin.DevicePolicyManager.EXTRA_PROVISIONING_ADMIN_EXTRAS_BUNDLE. Written out
    // rather than referenced for the same reason as the action strings in ProvisioningActivities:
    // the constant carries ApiSince = 23 but the bundle is only ever populated by the flows this
    // app supports, and the value is a frozen platform string.
    private const string ExtraAdminExtrasBundle = "android.app.extra.PROVISIONING_ADMIN_EXTRAS_BUNDLE";

    private const string Tag = "FleetHubAgent";

    /// <summary>Take the hub URL and enrollment secret out of a provisioning intent, if it
    /// carries them. Never throws: this runs on the path whose failure strands a
    /// freshly-wiped device in the setup wizard.</summary>
    internal static void Adopt(Context context, Intent? intent, AgentState state)
    {
        try
        {
            var extras = intent?.GetBundleExtra(ExtraAdminExtrasBundle);
            if (extras is null)
            {
                // Info, not Warn. A device provisioned by an MDM rather than by a FleetHub QR
                // legitimately has no bundle and gets its configuration from the policy instead.
                global::Android.Util.Log.Info(Tag,
                    "No provisioning extras in this intent; falling back to managed " +
                    "configuration and the setup screen");
                return;
            }

            var hub = Clean(extras.GetString(ManagedConfig.KeyHubUrl));
            if (hub is not null && state.SaveHubBaseOverride(hub))
            {
                AgentConfig.Configure(hub);
                // The URL is logged and the secret never is. One is what a technician needs to
                // read back off the device; the other is a fleet-wide credential, and logcat is
                // readable by adb and included in every bug report.
                global::Android.Util.Log.Info(Tag, $"Adopted hub URL from the QR: {hub}");
            }
            else if (hub is not null)
            {
                global::Android.Util.Log.Error(Tag,
                    "Could not store the hub URL from the QR; this device will report nowhere");
            }

            var secret = Clean(extras.GetString(ManagedConfig.KeyEnrollmentSecret));
            if (secret is not null)
            {
                global::Android.Util.Log.Info(Tag, state.SaveEnrollmentSecret(secret)
                    ? "Adopted the enrollment secret from the QR"
                    : "Could not store the enrollment secret from the QR; this device will " +
                      "report telemetry and never accept a command");
            }
        }
        catch (Exception e)
        {
            global::Android.Util.Log.Error(Tag, $"Could not read the provisioning extras: {e.Message}");
        }
    }

    /// <summary>Trimmed, or null when there is nothing usable. The hub trims before it mints, so
    /// this is about a bundle assembled by hand or by another tool.</summary>
    private static string? Clean(string? value) =>
        string.IsNullOrWhiteSpace(value) ? null : value.Trim();
}
