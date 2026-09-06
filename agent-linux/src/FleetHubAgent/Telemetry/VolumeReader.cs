using System.Globalization;

namespace FleetHubAgent.Telemetry;

/// <summary>
/// Per-volume capacity, appended to the ordinary sensor block as synthetic readings.
///
/// The identifier prefix "/volume/" is the contract with the hub -- see app.py's
/// _disk_volumes, which builds the Storage cards from exactly the "Total Space" and
/// "Used Space" Data sensors carrying it. The Windows agent's VolumeReader emits the same
/// pair for the same reason; this is the Linux half, and the two must agree because one hub
/// page renders both.
///
/// **Which mounts count is the whole problem on Linux.** A Linux box mounts dozens of
/// filesystems that are not storage: /proc, /sys, /run, cgroups, every squashfs a snap brings
/// with it (each 100% full, by design), and a bind mount that shows the same device twice.
/// Listing them turns the Storage card into noise and puts every snap-using machine at the top
/// of the fleet's "least free disk" list -- permanently, and with nothing anyone can do about
/// it. DriveInfo.DriveType is not enough of a filter here (it reports Fixed for squashfs), so
/// the filesystem TYPE is what decides, against a list of the real ones.
///
/// **Known limitation:** DriveInfo.GetDrives() statfs()es every mount point, so a hung NFS or
/// CIFS mount blocks this walk before the type filter can reject it -- on a server with a dead
/// file server, the telemetry loop stalls at its 5-second tick. It is the telemetry loop alone
/// (heartbeat and commands are separate loops for exactly this class of reason), so the machine
/// stays online and answerable; it stops reporting readings. Fixing it properly means parsing
/// /proc/mounts and statfs-ing only the survivors, on a bounded thread. Recorded in ROADMAP.MD
/// #22 rather than half-done here.
/// </summary>
public static class VolumeReader
{
    private const double BytesPerGb = 1024d * 1024d * 1024d;

    /// <summary>Filesystems that hold a machine's actual data.
    ///
    /// An allow-list, not a deny-list, and deliberately so: the set of pseudo-filesystems
    /// grows with every kernel release and with every container runtime, so a deny-list is
    /// wrong the moment it ships. A real filesystem missing from here shows up as a disk the
    /// console does not display -- visible, reportable, and one line to fix. The reverse
    /// failure is silent noise on every machine.</summary>
    private static readonly HashSet<string> RealFilesystems = new(StringComparer.Ordinal)
    {
        "ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "jfs", "reiserfs",
        "vfat", "exfat", "ntfs", "ntfs3",
    };

    /// <summary>Appends two Data sensors ("Total Space", "Used Space", both GB) per real
    /// volume. Never throws: a filesystem that disappears mid-enumeration, or one the service
    /// cannot stat, is skipped rather than costing us the whole sensor block.</summary>
    public static void Append(List<SensorReading> sensors)
    {
        DriveInfo[] drives;
        try { drives = DriveInfo.GetDrives(); }
        catch { return; }

        // One entry per DEVICE, keyed by mount point. A bind mount and a btrfs subvolume both
        // report the same device twice; counting a 2 TB disk twice would double this
        // machine's storage in the fleet totals.
        var seen = new HashSet<string>(StringComparer.Ordinal);

        foreach (var drive in drives)
        {
            double totalGb, usedGb;
            string mount;
            try
            {
                if (!drive.IsReady) continue;
                if (!RealFilesystems.Contains(drive.DriveFormat)) continue;

                var total = drive.TotalSize;
                if (total <= 0) continue;
                if (!seen.Add(drive.Name)) continue;

                // TotalFreeSpace, not AvailableFreeSpace. On ext4 the two differ by the 5%
                // root reserve, and "how full is this disk" is not "what may a non-root user
                // still write" -- reporting the latter shows every fresh disk as 5% used.
                totalGb = Math.Round(total / BytesPerGb, 1);
                usedGb = Math.Round((total - drive.TotalFreeSpace) / BytesPerGb, 1);
                mount = drive.Name;
            }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException)
            {
                continue;
            }

            // Lowercased and slash-normalised so the identifier is stable and sorts sensibly
            // ("/" first, then /boot, /home). The hub sorts the Storage cards by this string.
            var id = "/volume/" + mount.Trim('/').Replace('/', '-').ToLowerInvariant();
            if (id == "/volume/") id = "/volume/root";

            sensors.Add(Reading(mount, id, "Total Space", totalGb));
            sensors.Add(Reading(mount, id, "Used Space", usedGb));
        }
    }

    // Group/Type "Data" in GB, matching the Windows agent's synthetic volume sensors -- the
    // hub's readers key off Type, so these have to look native.
    private static SensorReading Reading(string mount, string hardwareId, string name, double gb) =>
        new()
        {
            Hardware = mount,
            HardwareId = hardwareId,
            Group = "Data",
            Name = name,
            Type = "Data",
            Value = gb,
            Text = gb.ToString("0.0", CultureInfo.InvariantCulture) + " GB",
        };
}
