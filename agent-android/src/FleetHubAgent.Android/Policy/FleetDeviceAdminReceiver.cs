using Android.App;
using Android.App.Admin;
using Android.Content;
using FleetHubAgent.Android.Platform;
using FleetHubAgent.State;

namespace FleetHubAgent.Android.Policy;

/// <summary>
/// The device-admin component. Its flattened name is what the provisioning QR points at, and
/// what makes this app eligible to become Device Owner at all.
///
/// **This class exists to BE NAMED more than to do anything.** A DeviceAdminReceiver is how
/// Android identifies a policy controller: the QR carries
/// `net.arkeanos.fleethub.agent/crc64....FleetDeviceAdminReceiver`, the setup wizard installs
/// the APK, verifies its signature against the checksum in the same QR, and hands ownership to
/// the component with that exact name. Get the name wrong and the device -- already factory
/// reset by then -- cannot finish provisioning, and finding out costs a second wipe. That is
/// why nothing in this repo writes the name out by hand: DeviceOwner.Component builds it from
/// this type, and the hub reads it from a setting the operator fills in once from
/// `adb shell dumpsys device_policy`.
///
/// **`android:permission="android.permission.BIND_DEVICE_ADMIN"` is not optional decoration.**
/// Without it the platform refuses to accept the component as an admin at all, and the
/// provisioning failure names neither the permission nor this file.
///
/// **The callbacks here are deliberately thin.** On Android 10 and above the real setup work
/// happens in <see cref="PolicyComplianceActivity"/>, which the framework starts once
/// provisioning is otherwise finished; `OnProfileProvisioningComplete` is the older path and
/// is not called on every flow. Both do the same idempotent thing rather than splitting the
/// work between them, because "which of these two fired" is not a question anyone should have
/// to answer while looking at a device that has just been wiped.
/// </summary>
// The label and description are what a person sees in Settings -> Device admin apps. They are
// resource references because android:description is a reference-typed attribute -- a literal
// there is the APT2259 failure that Resources/values/strings.xml already exists because of.
[BroadcastReceiver(
    Name = "net.arkeanos.fleethub.agent.FleetDeviceAdminReceiver",
    Permission = "android.permission.BIND_DEVICE_ADMIN",
    Label = "@string/device_admin_label",
    Description = "@string/device_admin_description",
    Exported = true)]
[MetaData("android.app.device_admin", Resource = "@xml/device_admin")]
[IntentFilter(["android.app.action.DEVICE_ADMIN_ENABLED"])]
public sealed class FleetDeviceAdminReceiver : DeviceAdminReceiver
{
    // An explicit Name, unlike every other component in this app. .NET Android otherwise
    // generates a mangled class name (`crc64abcdef...FleetDeviceAdminReceiver`) whose hash
    // depends on the assembly, so it can change between builds -- and this name is baked into
    // a printed QR code that a technician may still be scanning a year from now. A QR naming a
    // component that no longer exists sends a freshly wiped device into a provisioning failure
    // with no way back but another wipe. Pinning it is what makes the QR durable.
    //
    // It is also the string the hub stores in `provisioning.admin_component`; the two must
    // match character for character.
    public const string ComponentClassName = "net.arkeanos.fleethub.agent.FleetDeviceAdminReceiver";

    private const string Tag = "FleetHubAgent";

    /// <summary>Provisioning finished and this app is now Device Owner (the pre-Android-10
    /// path, and some OEM flows above it).</summary>
    public override void OnProfileProvisioningComplete(Context context, Intent intent)
    {
        base.OnProfileProvisioningComplete(context, intent);
        global::Android.Util.Log.Info(Tag, "Provisioning complete; finishing device-owner setup");
        FinishProvisioning(context, intent);
    }

    public override void OnEnabled(Context context, Intent intent)
    {
        base.OnEnabled(context, intent);
        // Logged at Info because it is the line that answers "did the QR actually take?" when
        // somebody is holding a device that looks identical either way.
        global::Android.Util.Log.Info(Tag,
            $"Device admin enabled; {DeviceOwner.Describe(context)}");
    }

    public override void OnDisabled(Context context, Intent intent)
    {
        base.OnDisabled(context, intent);
        // A Device Owner cannot be disabled from the device, so reaching this means the app was
        // an ordinary device admin, or ownership was cleared by `adb shell dpm remove-active-admin`
        // on a debug build. Either way the policy features stop working and the console must be
        // able to see why -- the next heartbeat drops `device_owner` from the capability report.
        global::Android.Util.Log.Warn(Tag,
            "Device admin disabled; policy features are no longer available on this device");
    }

    /// <summary>Idempotent, and called from both provisioning paths. Everything expensive is
    /// the service's job; this only adopts the QR's configuration, takes the powers and starts
    /// it.
    ///
    /// **The intent is a parameter because it carries the fleet's configuration.** The hub puts
    /// the hub URL and the enrollment secret in the QR's ADMIN_EXTRAS_BUNDLE, and this is the one
    /// moment they are handed to the app. Dropping the intent -- which this method did -- makes
    /// provisioning by QR complete successfully and produce a device that reports to the
    /// compiled-in placeholder hub and enrolls nowhere. See ProvisioningExtras.</summary>
    internal static void FinishProvisioning(Context context, Intent? intent)
    {
        // BEFORE the service starts, not after. The hub URL is read once when the loops are
        // composed, so a secret and a URL adopted afterwards would not be picked up until
        // something restarted the service -- on a device nobody is holding.
        ProvisioningExtras.Adopt(context, intent, new AgentState(new AndroidStateStore(context)));

        // Started before the powers are applied rather than after, and on purpose: the service
        // is what makes the device appear in the console, and a failure in ApplyBaseline must
        // not be the reason a freshly provisioned device never checks in. ApplyBaseline runs
        // again from the service's own startup anyway.
        AgentService.Start(context);
    }
}
