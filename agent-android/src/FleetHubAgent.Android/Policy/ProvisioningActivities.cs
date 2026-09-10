using Android.App;
using Android.Content;
using Android.OS;

namespace FleetHubAgent.Android.Policy;

/// <summary>
/// The two activities Android 10+ requires a device policy controller to answer, without which
/// QR provisioning fails outright.
///
/// **Both are mandatory, and the failure mode is the worst one this project has.** From API 29
/// the setup wizard starts <c>ACTION_GET_PROVISIONING_MODE</c> to ask the DPC what kind of
/// management it wants, and <c>ACTION_ADMIN_POLICY_COMPLIANCE</c> once the framework side is
/// done. A DPC that does not handle both does not provision -- and by the time that is
/// discovered the device has already been factory reset, so the cost of finding out is a
/// second wipe and everything that was on it the first time.
///
/// They live in one file because they are one contract: neither means anything without the
/// other, and splitting them would make it possible to delete half of it.
///
/// **`BIND_DEVICE_ADMIN` on both, and `Exported = true`.** Exported because the caller is the
/// setup wizard, in another process; the permission is what stops any other app starting them.
/// The system holds that permission and nothing else does, so the pair is narrower than it
/// looks.
///
/// The action strings are written as literals rather than as
/// <c>DevicePolicyManager.ActionGetProvisioningMode</c>. Attribute arguments must be compile
/// time constants, which those fields are -- but they carry <c>ApiSince = 29</c>, and this app
/// targets API 26, so referencing them from an attribute is a platform-compatibility warning
/// about code that is not code. The values are frozen platform constants; the risk of writing
/// them out is nil and the comment says what they are.
/// </summary>
[Activity(
    Name = "net.arkeanos.fleethub.agent.GetProvisioningModeActivity",
    Permission = "android.permission.BIND_DEVICE_ADMIN",
    Exported = true,
    Theme = "@android:style/Theme.NoDisplay")]
[IntentFilter(["android.app.action.GET_PROVISIONING_MODE"],
    Categories = [Intent.CategoryDefault])]
public sealed class GetProvisioningModeActivity : Activity
{
    // android.app.admin.DevicePolicyManager.EXTRA_PROVISIONING_MODE (API 29).
    private const string ExtraProvisioningMode = "android.app.extra.PROVISIONING_MODE";
    // ...ALLOWED_PROVISIONING_MODES: an ArrayList<Integer> of the modes the wizard will accept.
    private const string ExtraAllowedModes = "android.app.extra.PROVISIONING_ALLOWED_PROVISIONING_MODES";
    // ...PROVISIONING_MODE_FULLY_MANAGED_DEVICE. The only mode this agent supports.
    private const int ModeFullyManagedDevice = 1;
    // ...PROVISIONING_MODE_MANAGED_PROFILE, listed only so the log line can name what was
    // offered. A work profile is explicitly out of scope (roadmap #23): this agent manages a
    // company-owned device, and half a device is not a machine the console can speak for.
    private const int ModeManagedProfile = 2;

    protected override void OnCreate(Bundle? savedInstanceState)
    {
        base.OnCreate(savedInstanceState);

        // The extra is an ArrayList<Integer>, so it arrives as boxed Java integers rather than
        // as ints -- unwrapped once here so nothing below has to remember that.
        var allowed = (Intent?.GetIntegerArrayListExtra(ExtraAllowedModes) ?? [])
            .Where(i => i is not null).Select(i => i!.IntValue()).ToList();
        var offered = allowed.Count == 0 ? "none" : string.Join(", ", allowed);
        global::Android.Util.Log.Info("FleetHubAgent",
            $"Provisioning mode requested; the wizard offered [{offered}]");

        // A wizard that will not offer a fully managed device is offering a work profile, and
        // answering with a mode it did not offer fails provisioning with a message about the
        // DPC rather than about the choice. Refusing here, with the reason in the log, is the
        // one chance to say WHY: this is a company-owned-device agent, and a work profile is
        // out of scope by decision rather than by omission (roadmap #23).
        //
        // An EMPTY list is not a refusal. It means the wizard sent no restriction at all, which
        // is what several OEM flows do, and treating silence as "fully managed is not allowed"
        // would fail provisioning on exactly the devices that would have worked.
        if (allowed.Count > 0 && !allowed.Contains(ModeFullyManagedDevice))
        {
            global::Android.Util.Log.Error("FleetHubAgent",
                $"This device offered only {(allowed.Contains(ModeManagedProfile) ? "a managed profile" : "an unknown mode")}. " +
                "The FleetHub agent manages fully managed (device owner) devices only.");
            SetResult(Result.Canceled);
            Finish();
            return;
        }

        var result = new Intent();
        result.PutExtra(ExtraProvisioningMode, ModeFullyManagedDevice);
        SetResult(Result.Ok, result);
        Finish();
    }
}

/// <summary>
/// Called once the framework has finished its side of provisioning: the DPC's chance to apply
/// its policy and say it is satisfied.
///
/// Returning <c>Result.Ok</c> is what completes provisioning. Returning anything else, or
/// throwing, leaves the device in the setup wizard -- so everything in here is wrapped and the
/// result is set regardless. **A device that cannot get past this screen has to be factory
/// reset again**, which makes "fail closed" exactly the wrong instinct: a device that is
/// managed but has not yet had one policy applied is worth far more than a brick, and the
/// agent re-applies its baseline on every service start anyway.
/// </summary>
[Activity(
    Name = "net.arkeanos.fleethub.agent.PolicyComplianceActivity",
    Permission = "android.permission.BIND_DEVICE_ADMIN",
    Exported = true,
    Theme = "@android:style/Theme.NoDisplay")]
[IntentFilter(["android.app.action.ADMIN_POLICY_COMPLIANCE"],
    Categories = [Intent.CategoryDefault])]
public sealed class PolicyComplianceActivity : Activity
{
    protected override void OnCreate(Bundle? savedInstanceState)
    {
        base.OnCreate(savedInstanceState);

        try
        {
            global::Android.Util.Log.Info("FleetHubAgent",
                $"Policy compliance check; {DeviceOwner.Describe(this)}");
            FleetDeviceAdminReceiver.FinishProvisioning(this, Intent);
        }
        catch (Exception e)
        {
            // See the class docstring: a throw here strands the device in the setup wizard,
            // and the only way out of that is another factory reset.
            global::Android.Util.Log.Error("FleetHubAgent",
                $"Could not finish device-owner setup, continuing anyway: {e.Message}");
        }

        SetResult(Result.Ok);
        Finish();
    }
}
