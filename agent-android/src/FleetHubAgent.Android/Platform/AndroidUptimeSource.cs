using Android.OS;
using FleetHubAgent.Telemetry;

namespace FleetHubAgent.Android.Platform;

/// <summary>
/// How long the device has been up, from SystemClock.ElapsedRealtime().
///
/// **ElapsedRealtime, not UptimeMillis, and the difference is the whole file.** UptimeMillis
/// stops counting while the device is in deep sleep, which for a phone or a tablet on a shelf
/// is most of the time -- a device up for a week would report a few hours, and the console's
/// uptime column would say a fleet reboots constantly when nothing had rebooted at all.
/// ElapsedRealtime includes sleep, so it answers the question an operator is actually asking:
/// how long since this device last booted.
///
/// The Linux agent makes the opposite-looking choice for the same reason -- it reads
/// /proc/uptime specifically to AVOID a monotonic tick count that excludes suspend. Both are
/// the same rule: report the machine's uptime, not the process's.
/// </summary>
internal sealed class AndroidUptimeSource : IUptimeSource
{
    public long? UptimeSeconds()
    {
        try
        {
            var ms = SystemClock.ElapsedRealtime();
            return ms > 0 ? ms / 1000 : null;
        }
        catch { return null; }
    }
}
