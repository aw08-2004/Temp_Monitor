using Microsoft.Extensions.Logging;

namespace FleetHubAgent.Telemetry;

/// <summary>
/// Reads the machine's hardware and OS identity once at startup.
///
/// Once, like the Windows agent, and correct for the same reason: a machine that is re-imaged
/// or dist-upgraded across a major release reboots to get there, and this process starts again
/// with it.
///
/// **serial_number is the field that matters most, and it is the one that needs root.**
/// /sys/class/dmi/id/product_serial is 0400 root-only (the rest of the DMI tree is
/// world-readable), because a machine serial is a tracking identifier. The hub uses it to
/// collapse duplicates -- resolve_serial_group() merges an offline machine into an online one
/// reporting the same serial, which is what makes a hostname change survivable (see
/// RenameExecutor). An agent that cannot read it renames itself into a permanent duplicate.
/// That is the argument for this service running as root rather than as a nobody user with
/// a handful of capabilities.
/// </summary>
public static class SystemInfo
{
    private const string DmiRoot = "/sys/class/dmi/id";

    public static SystemIdentity Read(ILogger log)
    {
        var identity = new SystemIdentity
        {
            SerialNumber = Clean(ReadFile(Path.Combine(DmiRoot, "product_serial"))),
            Model = Clean(ReadFile(Path.Combine(DmiRoot, "product_name"))),
            Manufacturer = Clean(ReadFile(Path.Combine(DmiRoot, "sys_vendor"))),
            AssetTag = Clean(ReadFile(Path.Combine(DmiRoot, "chassis_asset_tag"))),
            OsArchitecture = System.Runtime.InteropServices.RuntimeInformation.OSArchitecture.ToString(),
        };

        var release = ReadOsRelease();
        // PRETTY_NAME is the caption an operator should see ("Ubuntu 24.04.2 LTS"), and it is
        // also what the hub buckets on. Its _OS_MATCHES table knows ubuntu/debian/rhel/centos/
        // fedora/suse/alma/rocky and the bare word "linux" -- which covers most PRETTY_NAMEs
        // including "Arch Linux" and "Linux Mint", but NOT e.g. "Pop!_OS 22.04 LTS", which
        // buckets as unknown. That is a hub-side gap, recorded in ROADMAP.MD #22 rather than
        // papered over here: rewriting the caption to smuggle the word "Linux" in would put a
        // string in front of an operator that the machine never said.
        identity.OsCaption = release.GetValueOrDefault("PRETTY_NAME")
                             ?? release.GetValueOrDefault("NAME")
                             ?? "Linux";
        identity.OsVersion = release.GetValueOrDefault("VERSION_ID");
        // The kernel release, in the field the hub calls os_build. Safe to send despite
        // normalize_os parsing that field as a Windows build number: it only does so when the
        // caption is Windows-shaped or unrecognised, and a Linux caption short-circuits it.
        identity.OsBuild = Clean(ReadFile("/proc/sys/kernel/osrelease"));

        if (string.IsNullOrEmpty(identity.SerialNumber))
        {
            // Warned, not fatal, and warned LOUDLY -- see the class note. The usual causes are
            // running unprivileged and a VM whose hypervisor sets no serial.
            log.LogWarning(
                "No DMI serial number readable at {Path}. The hub uses it to collapse " +
                "duplicate machines, so a hostname change on this box would leave two " +
                "entries in the console.", Path.Combine(DmiRoot, "product_serial"));
        }

        log.LogInformation("Identity: {Manufacturer} {Model} - {Os} (kernel {Kernel})",
            identity.Manufacturer ?? "?", identity.Model ?? "?",
            identity.OsCaption, identity.OsBuild ?? "?");

        return identity;
    }

    /// <summary>Parse /etc/os-release into a dictionary, honouring the quoting the spec allows.
    ///
    /// /etc/os-release is a symlink into /usr/lib on a stateless system and a real file
    /// everywhere else; reading the /etc path follows either. Internal so a test can hold the
    /// quoting cases -- PRETTY_NAME is quoted, VERSION_ID sometimes is, and NAME may contain
    /// an escaped quote.</summary>
    internal static Dictionary<string, string> ParseOsRelease(IEnumerable<string> lines)
    {
        var map = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (var raw in lines)
        {
            var line = raw.Trim();
            if (line.Length == 0 || line[0] == '#') continue;
            var eq = line.IndexOf('=');
            if (eq <= 0) continue;

            var key = line[..eq].Trim();
            var value = line[(eq + 1)..].Trim();
            if (value.Length >= 2 &&
                ((value[0] == '"' && value[^1] == '"') || (value[0] == '\'' && value[^1] == '\'')))
            {
                value = value[1..^1].Replace("\\\"", "\"").Replace("\\\\", "\\");
            }
            if (value.Length > 0) map[key] = value;
        }
        return map;
    }

    private static Dictionary<string, string> ReadOsRelease()
    {
        foreach (var path in new[] { "/etc/os-release", "/usr/lib/os-release" })
        {
            try
            {
                if (File.Exists(path)) return ParseOsRelease(File.ReadAllLines(path));
            }
            catch { /* try the next one */ }
        }
        return new Dictionary<string, string>(StringComparer.Ordinal);
    }

    private static string? ReadFile(string path)
    {
        try { return File.Exists(path) ? File.ReadAllText(path).Trim() : null; }
        catch { return null; }
    }

    /// <summary>Drop the placeholder strings vendors ship in DMI.
    ///
    /// A field left unset by the board vendor comes back as literal text -- "To Be Filled By
    /// O.E.M.", "Default string", "None", "Not Specified" -- and every machine from that
    /// vendor reports the SAME one. Passed through, they become a serial number shared by
    /// dozens of machines, which is precisely the input that makes the hub's duplicate-serial
    /// merge collapse unrelated boxes into each other. Null is the honest answer.</summary>
    internal static string? Clean(string? value)
    {
        var text = (value ?? "").Trim();
        if (text.Length == 0) return null;
        foreach (var placeholder in Placeholders)
            if (text.Equals(placeholder, StringComparison.OrdinalIgnoreCase)) return null;
        // Some vendors fill the field with a single repeated character instead.
        if (text.All(c => c == '0' || c == '.' || c == '-')) return null;
        return text;
    }

    private static readonly string[] Placeholders =
    {
        "To Be Filled By O.E.M.", "To be filled by O.E.M.", "Default string", "None",
        "Not Specified", "Not Available", "Unknown", "System Serial Number",
        "Chassis Asset Tag", "Asset-1234567890", "N/A", "INVALID",
    };
}
