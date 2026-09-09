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
    /// operator typed on the setup screen, then the compiled-in default.</summary>
    public static void ApplyHubUrl(Context context, AgentState state)
    {
        AgentConfig.Configure(HubUrl(context) ?? state.LoadHubBaseOverride());
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
