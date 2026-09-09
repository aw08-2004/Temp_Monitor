using Android.App.Admin;
using Android.Content;
using Microsoft.Extensions.Logging;
using FleetHubAgent.Fleet;

namespace FleetHubAgent.Android.Policy;

/// <summary>
/// Locking and erasing this device -- roadmap #23 phase H.
///
/// The platform half of <see cref="IDeviceSecurity"/>, and deliberately the whole of it: every
/// decision about whether to do either of these, what to answer, and how long to wait first is
/// in the executors. This file knows two API calls and the names of the flags they take.
///
/// **`lockNow` needs the force-lock policy and `wipeData` needs wipe-data**, both declared in
/// `device_admin.xml` since phase A. That was not foresight about this phase so much as the
/// one lesson provisioning teaches: a device owner cannot be re-prompted for a policy it did
/// not declare, so a missing one is discovered on a device that has already been factory reset
/// and costs a second wipe to add.
/// </summary>
public sealed class AndroidDeviceSecurity(Context context, ILogger log) : IDeviceSecurity
{
    /// <summary>Evaluated live rather than captured, for the same reason DeviceOwner.Features
    /// is: `adb shell dpm set-device-owner` can grant ownership to a running process, and a
    /// stale answer here would refuse a lock on a device that can perform one.</summary>
    public bool CanEnforce => DeviceOwner.IsManaged(context);

    public bool Lock()
    {
        var dpm = Manager();
        if (dpm is null) return false;
        try
        {
            // The no-argument overload: lock the screen as it is configured. The variant that
            // takes flags can also lock a work profile's challenge separately, which is a
            // work-profile concept and this is a fully managed device.
            dpm.LockNow();
            return true;
        }
        catch (Exception e)
        {
            log.LogWarning("lockNow refused: {Msg}", e.Message);
            return false;
        }
    }

    public void Wipe(bool clearResetProtection)
    {
        var dpm = Manager();
        if (dpm is null)
        {
            log.LogError("No device-policy service; this device has NOT been erased");
            return;
        }

        // WIPE_RESET_PROTECTION_DATA clears the factory-reset protection block along with the
        // data, so the device can be set up again by anybody. The hub decides this per request
        // and says which it is doing -- see wipe.py. Requesting the flag on a device whose
        // build has no FRP is harmless; the platform ignores what it cannot act on.
        var flags = clearResetProtection
            ? WipeDataFlags.WipeResetProtectionData
            : WipeDataFlags.None;

        try
        {
            // Does not return. Anything after this line runs only if the platform refused.
            dpm.WipeData(flags);
            log.LogError("wipeData returned; this device has NOT been erased");
        }
        catch (Exception e)
        {
            log.LogError(e, "wipeData refused; this device has NOT been erased");
        }
    }

    private DevicePolicyManager? Manager()
        => context.GetSystemService(Context.DevicePolicyService) as DevicePolicyManager;
}
