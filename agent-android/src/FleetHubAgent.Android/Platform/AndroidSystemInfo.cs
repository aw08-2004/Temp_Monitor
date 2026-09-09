using Android.Content;
using Android.Provider;
using Microsoft.Extensions.Logging;
using FleetHubAgent.Telemetry;

namespace FleetHubAgent.Android.Platform;

/// <summary>
/// Reads the device's hardware and OS identity once at startup.
///
/// Once, like every other agent, and correct for the same reason: a device that is factory
/// reset or takes a major OS upgrade reboots to get there, and this process starts again with
/// it.
///
/// **Two of the hub's fields do not carry what their names say, and both are deliberate.**
///
/// `serial_number` carries the SSAID, not a hardware serial. An ordinary app cannot read a
/// hardware serial on any modern Android at any permission level -- the full argument, and the
/// junk values that have to be filtered out of the SSAID, are on
/// IdentityCleaning.CleanStableId. This is the field the hub collapses duplicates on, so it is
/// the most consequential thing this file produces.
///
/// `os_build` carries the build display id (Build.DISPLAY, e.g. "AP4A.250105.002"), in the
/// field the hub calls a Windows build number. Safe to send for the same reason the Linux
/// agent's kernel release is: normalize_os only parses that field as a build number when the
/// caption is Windows-shaped or unrecognised, and it takes the caption's word first.
///
/// **The OS caption buckets as "unknown" in the console and is reported honestly anyway.**
/// app.py's _OS_MATCHES knows windows/linux and their distributions; "Android 15" matches
/// none of them, so an Android device counts in the Dashboard's `unknown` bucket. The
/// alternative -- smuggling the word "Linux" into the caption, which is even technically
/// defensible for an Android kernel -- would put a string in front of an operator that the
/// device never said, and would file phones in with the servers. Adding an `android` bucket is
/// a one-line hub change recorded in ROADMAP.MD #23; until someone makes it, the honest
/// caption and a wrong bucket beat a right bucket and a lie.
/// </summary>
internal static class AndroidSystemInfo
{
    public static SystemIdentity Read(Context context, ILogger log)
    {
        var identity = new SystemIdentity
        {
            SerialNumber = ReadStableId(context, log),
            Model = IdentityCleaning.Clean(global::Android.OS.Build.Model),
            Manufacturer = IdentityCleaning.Clean(global::Android.OS.Build.Manufacturer),

            // There is no asset tag on Android. An MDM can supply one through managed
            // configuration, which is the only place a real asset number could come from --
            // inventing one from a Build field would put a number in the console's Asset Tag
            // column that matches nothing in the asset register.
            AssetTag = ManagedConfig.AssetTag(context),

            OsCaption = "Android " + (global::Android.OS.Build.VERSION.Release ?? "?"),
            OsVersion = global::Android.OS.Build.VERSION.Release,
            OsBuild = IdentityCleaning.Clean(global::Android.OS.Build.Display),
            OsArchitecture = PrimaryAbi(),
        };

        log.LogInformation("Identity: {Manufacturer} {Model} - {Os} (build {Build}, {Abi})",
            identity.Manufacturer ?? "?", identity.Model ?? "?",
            identity.OsCaption, identity.OsBuild ?? "?", identity.OsArchitecture ?? "?");

        return identity;
    }

    /// <summary>The value sent as serial_number: the SSAID, filtered.
    ///
    /// Warned LOUDLY when there is none, because the consequences are invisible until someone
    /// renames the device -- at which point it becomes a permanent duplicate in the console,
    /// with no way back. RenameExecutor refuses outright in that state rather than relying on
    /// anyone having read this log line.</summary>
    private static string? ReadStableId(Context context, ILogger log)
    {
        string? raw = null;
        try
        {
            raw = Settings.Secure.GetString(context.ContentResolver!, Settings.Secure.AndroidId);
        }
        catch (Exception e)
        {
            log.LogWarning("Could not read the device's stable id: {Msg}", e.Message);
        }

        var cleaned = IdentityCleaning.CleanStableId(raw);
        if (cleaned is null)
        {
            log.LogWarning(
                "No usable stable id for this device (read {Raw}). The hub uses it to collapse " +
                "duplicate machines, so this device cannot be renamed safely and will appear " +
                "twice if it ever is. Commands other than rename are unaffected.",
                string.IsNullOrEmpty(raw) ? "nothing" : "a value this agent rejects as shared");
        }
        return cleaned;
    }

    /// <summary>The ABI the device actually runs, for the hub's os_arch column.
    ///
    /// SupportedAbis[0] rather than Build.CpuAbi: the latter is deprecated and, on a 64-bit
    /// device running a 32-bit process, reports the process's ABI rather than the device's.
    /// The console's question is what the hardware is.</summary>
    private static string? PrimaryAbi()
    {
        try
        {
            var abis = global::Android.OS.Build.SupportedAbis;
            return abis is { Count: > 0 } ? abis[0] : null;
        }
        catch { return null; }
    }
}
