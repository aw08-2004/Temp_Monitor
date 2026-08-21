using System.Management;
using Microsoft.Extensions.Logging;

namespace TempMonitorAgent.Telemetry;

/// <summary>
/// Reads BIOS/chassis identity via WMI once at startup (values don't change at
/// runtime). Reads serial (Win32_BIOS), model and manufacturer (Win32_ComputerSystem),
/// asset tag (Win32_SystemEnclosure.SMBIOSAssetTag),
/// and service tag (Win32_SystemEnclosure.SerialNumber -- the chassis serial, distinct
/// from the BIOS serial, and where Dell's Service Tag lives) with the same placeholder
/// filtering. Also reads the operating system (Win32_OperatingSystem) and exposes system
/// uptime.
/// </summary>
public static class SystemInfo
{
    private static readonly string[] PlaceholderAssetTags =
        { "default string", "no asset", "to be filled", "invalid" };

    /// <summary>Whitebox/OEM-unset values for Win32_ComputerSystem.Manufacturer:
    /// manufacturer is what BIOS and firmware management dispatches on, so an
    /// unknown vendor must read as unknown
    /// rather than as a brand nobody can query.</summary>
    private static readonly string[] PlaceholderManufacturers =
        PlaceholderAssetTags.Concat(new[] { "system manufacturer", "o.e.m." }).ToArray();

    /// <summary>Is this a manufacturer string worth dispatching on? Shared with
    /// <see cref="Bios.BiosReader"/> (roadmap #9) rather than copied: the vendor the firmware
    /// reader picks and the manufacturer shown on the machine page must be the same
    /// judgement, or a machine reads "Dell" on screen while reporting no manageable BIOS.</summary>
    public static bool IsRealManufacturer(string? manufacturer)
    {
        var value = (manufacturer ?? "").Trim();
        if (value.Length == 0) return false;
        var lowered = value.ToLowerInvariant();
        return !PlaceholderManufacturers.Any(p => lowered.Contains(p));
    }

    public static SystemIdentity Read(ILogger logger)
    {
        var info = new SystemIdentity();
        try
        {
            info.SerialNumber = Clean(QueryFirst("Win32_BIOS", "SerialNumber"));
            info.Model = Clean(QueryFirst("Win32_ComputerSystem", "Model"));

            var manufacturer = (QueryFirst("Win32_ComputerSystem", "Manufacturer") ?? "").Trim();
            if (IsRealManufacturer(manufacturer)) info.Manufacturer = manufacturer;

            var assetTag = (QueryFirst("Win32_SystemEnclosure", "SMBIOSAssetTag") ?? "").Trim();
            if (assetTag.Length > 0 &&
                !PlaceholderAssetTags.Any(p => assetTag.ToLowerInvariant().Contains(p)))
            {
                info.AssetTag = assetTag;
            }

            var serviceTag = (QueryFirst("Win32_SystemEnclosure", "SerialNumber") ?? "").Trim();
            if (serviceTag.Length > 0 &&
                !PlaceholderAssetTags.Any(p => serviceTag.ToLowerInvariant().Contains(p)))
            {
                info.ServiceTag = serviceTag;
            }
        }
        catch (Exception e)
        {
            logger.LogWarning(e, "[system-info] Could not read BIOS/system info");
        }

        // Its own try/catch, and deliberately AFTER the block above: an OS read that throws
        // must not cost the machine its serial and model, which is what a single shared
        // catch around both would have done.
        try
        {
            var os = QueryFirstObject("Win32_OperatingSystem",
                                      "Caption", "Version", "BuildNumber", "OSArchitecture");
            info.OsCaption = Clean(os.GetValueOrDefault("Caption"));
            info.OsVersion = Clean(os.GetValueOrDefault("Version"));
            info.OsBuild = Clean(os.GetValueOrDefault("BuildNumber"));
            info.OsArchitecture = Clean(os.GetValueOrDefault("OSArchitecture"));
        }
        catch (Exception e)
        {
            logger.LogWarning(e, "[system-info] Could not read operating system info");
        }
        return info;
    }

    private static string? QueryFirst(string wmiClass, string property)
    {
        using var searcher = new ManagementObjectSearcher($"SELECT {property} FROM {wmiClass}");
        foreach (ManagementObject obj in searcher.Get())
        {
            using (obj)
            {
                return obj[property]?.ToString();
            }
        }
        return null;
    }

    /// <summary>Several properties of one WMI class in ONE round trip.
    ///
    /// <see cref="QueryFirst"/> queries per property, which is fine for the identity fields
    /// above because they come from three different classes anyway. Four separate queries
    /// against Win32_OperatingSystem would be three round trips of pure waste, on a class
    /// that is not free to instantiate.</summary>
    private static Dictionary<string, string?> QueryFirstObject(
        string wmiClass, params string[] properties)
    {
        var result = new Dictionary<string, string?>();
        using var searcher = new ManagementObjectSearcher(
            $"SELECT {string.Join(", ", properties)} FROM {wmiClass}");
        foreach (ManagementObject obj in searcher.Get())
        {
            using (obj)
            {
                foreach (var property in properties)
                {
                    result[property] = obj[property]?.ToString();
                }
                break;   // one OS per machine; the rest of the enumeration is not interesting
            }
        }
        return result;
    }

    private static string? Clean(string? s)
    {
        var t = (s ?? "").Trim();
        return t.Length == 0 ? null : t;
    }

    /// <summary>Seconds since boot (kernel32 GetTickCount64, via Environment.TickCount64).</summary>
    public static long? UptimeSeconds()
    {
        try { return (long)Math.Round(Environment.TickCount64 / 1000.0); }
        catch { return null; }
    }
}
