using System.Management;
using Microsoft.Extensions.Logging;

namespace TempMonitorAgent.Telemetry;

/// <summary>
/// Reads BIOS/chassis identity via WMI once at startup (values don't change at
/// runtime). Mirrors companion.py get_system_info: serial (Win32_BIOS), model
/// (Win32_ComputerSystem), asset tag (Win32_SystemEnclosure.SMBIOSAssetTag), and
/// service tag (Win32_SystemEnclosure.SerialNumber -- the chassis serial, distinct from
/// the BIOS serial, and where Dell's Service Tag lives) with the same placeholder
/// filtering. Also exposes system uptime.
/// </summary>
public static class SystemInfo
{
    private static readonly string[] PlaceholderAssetTags =
        { "default string", "no asset", "to be filled", "invalid" };

    public static SystemIdentity Read(ILogger logger)
    {
        var info = new SystemIdentity();
        try
        {
            info.SerialNumber = Clean(QueryFirst("Win32_BIOS", "SerialNumber"));
            info.Model = Clean(QueryFirst("Win32_ComputerSystem", "Model"));

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
