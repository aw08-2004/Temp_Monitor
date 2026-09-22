using System.Collections.Concurrent;
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
/// it. So the filesystem TYPE is what decides, against a list of the real ones.
///
/// **A hung mount can no longer stall the telemetry loop, and that is what this walk is
/// shaped around** (roadmap #22). The old version started from DriveInfo.GetDrives(), which
/// statfs()es every mount point before any filter can run -- so a dead NFS or CIFS server
/// blocked the whole walk in an uninterruptible syscall and the machine silently stopped
/// reporting readings while staying online and answerable. Two changes fix it:
///
///   * the type filter now runs against <see cref="MountTable"/>, which reads the kernel's own
///     mount table and touches no filesystem, so a dead mount of the wrong type costs nothing
///     at all; and
///   * the stat of each survivor runs on the thread pool with a deadline. A mount that does
///     not answer within it is left out of this report rather than blocking it.
///
/// **A probe that timed out is never started again while it is still running.** This is the
/// part that matters more than the deadline: statfs on a hung NFS mount is uninterruptible, so
/// the thread that made the call stays blocked until the server comes back -- possibly
/// forever. Without <see cref="_inFlight"/>, every tick would start another one and a machine
/// with one dead mount would exhaust the thread pool in an afternoon. The entry clears itself
/// if the mount ever answers, so a file server that comes back is picked up on the next tick
/// with no restart.
/// </summary>
public static class VolumeReader
{
    private const double BytesPerGb = 1024d * 1024d * 1024d;

    /// <summary>How long the whole stat pass may take. Generous for a local disk (a warm
    /// statfs is microseconds) and far below the telemetry loop's five-second tick, so a
    /// machine with a dead mount reports its healthy volumes on the same cadence as one with
    /// none.</summary>
    internal const int ProbeTimeoutMs = 2000;

    /// <summary>Filesystems that hold a machine's actual data.
    ///
    /// An allow-list, not a deny-list, and deliberately so: the set of pseudo-filesystems
    /// grows with every kernel release and with every container runtime, so a deny-list is
    /// wrong the moment it ships. A real filesystem missing from here shows up as a disk the
    /// console does not display -- visible, reportable, and one line to fix. The reverse
    /// failure is silent noise on every machine.
    ///
    /// **Network filesystems are deliberately absent**, which is now a load-bearing omission
    /// rather than an oversight: an NFS or CIFS mount's free space belongs to the file server,
    /// not to this machine, so reporting it would double-count one server across every client
    /// in the fleet -- and it is exactly the mount that hangs.</summary>
    private static readonly HashSet<string> RealFilesystems = new(StringComparer.Ordinal)
    {
        "ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "jfs", "reiserfs",
        "vfat", "exfat", "ntfs", "ntfs3",
    };

    /// <summary>Stat probes that have not come back, keyed by mount point. See the class
    /// remarks: this is what stops one hung mount consuming a thread per tick.</summary>
    private static readonly ConcurrentDictionary<string, byte> _inFlight = new();

    /// <summary>What a mount answered, or null when it did not answer in time.</summary>
    private readonly record struct Capacity(double TotalGb, double UsedGb);

    /// <summary>Appends two Data sensors ("Total Space", "Used Space", both GB) per real
    /// volume. Never throws: a filesystem that disappears mid-enumeration, or one the service
    /// cannot stat, is skipped rather than costing us the whole sensor block.</summary>
    public static void Append(List<SensorReading> sensors)
    {
        var candidates = Candidates(MountTable.Read());
        if (candidates.Count == 0) return;

        foreach (var (mount, capacity) in Measure(candidates))
        {
            // Lowercased and slash-normalised so the identifier is stable and sorts sensibly
            // ("/" first, then /boot, /home). The hub sorts the Storage cards by this string.
            var id = "/volume/" + mount.Trim('/').Replace('/', '-').ToLowerInvariant();
            if (id == "/volume/") id = "/volume/root";

            sensors.Add(Reading(mount, id, "Total Space", capacity.TotalGb));
            sensors.Add(Reading(mount, id, "Used Space", capacity.UsedGb));
        }
    }

    /// <summary>The mounts worth stat-ing: real filesystems, one per DEVICE.
    ///
    /// **One entry per device, not per mount point.** A bind mount and a btrfs subvolume both
    /// appear twice in the mount table on the same device, and counting a 2 TB disk twice
    /// would double this machine's storage in the fleet totals. The first mount point wins,
    /// which in kernel order is the one mounted earliest -- "/" rather than the bind of a
    /// directory inside it.
    ///
    /// Deduplicates by major:minor identity from /proc/self/mountinfo rather than by the
    /// device string, because device names like /dev/sda2 and /dev/dm-0 can refer to the
    /// same underlying block device through different paths, while a device name alone
    /// cannot distinguish aliases and bind mounts from genuinely separate volumes.
    ///
    /// Internal so a test can pin the filtering against a real mount table without stat-ing
    /// anything.</summary>
    internal static IReadOnlyList<MountEntry> Candidates(IReadOnlyList<MountEntry> mounts)
    {
        var seen = new HashSet<string>(StringComparer.Ordinal);
        var keep = new List<MountEntry>();
        foreach (var mount in mounts)
        {
            if (!RealFilesystems.Contains(mount.FsType)) continue;
            // Use major:minor as the stable filesystem identity when available (mountinfo);
            // fall back to the device string for the older /proc/mounts format.
            var key = mount.DeviceMajor > 0 || mount.DeviceMinor > 0
                ? $"{mount.DeviceMajor}:{mount.DeviceMinor}"
                : mount.Device;
            if (!seen.Add(key)) continue;
            keep.Add(mount);
        }
        return keep;
    }

    /// <summary>Stat every candidate in parallel, and take whatever answered inside the
    /// deadline. The order of the result follows the mount table, so the console's Storage
    /// cards keep their stable order whether or not a mount was slow this tick.</summary>
    private static List<(string Mount, Capacity Capacity)> Measure(IReadOnlyList<MountEntry> candidates)
    {
        var probes = new List<(string Mount, Task<Capacity?> Task)>(candidates.Count);
        foreach (var mount in candidates)
        {
            // Key includes the mount ID so a replacement filesystem at the same path
            // (e.g. a remount or container recreation) gets its own probe entry. Without
            // it, completion or timeout cleanup from an older task could remove a newer
            // instance's entry, and the new filesystem would silently stop being probed.
            var inFlightKey = mount.MountId > 0
                ? $"{mount.MountPoint}@{mount.MountId}"
                : mount.MountPoint;
            // A probe already blocked on this mount from an earlier tick: leave it alone. It
            // will either come back and clear itself, or this mount stays unreported for as
            // long as its server is dead -- which is the honest answer, and the one that
            // costs one thread rather than one per tick.
            if (!_inFlight.TryAdd(inFlightKey, 0)) continue;
            var path = mount.MountPoint;
            var key = inFlightKey;
            probes.Add((path, Task.Run(() =>
            {
                try { return Stat(path); }
                finally { _inFlight.TryRemove(key, out _); }
            })));
        }

        if (probes.Count == 0) return new List<(string, Capacity)>();

        // One deadline for the whole pass rather than one each: the probes run concurrently,
        // so a per-mount budget would let a machine with ten mounts spend ten times as long
        // for no extra information.
        try { Task.WaitAll(probes.Select(p => p.Task).ToArray(), ProbeTimeoutMs); }
        catch { /* an individual probe's exception is read below, per probe */ }

        var measured = new List<(string, Capacity)>(probes.Count);
        foreach (var (mount, task) in probes)
        {
            // Completed AND successful: a probe still running is a mount that did not answer,
            // and a faulted one is a mount we could not read. Neither is reported, because a
            // Storage card that silently shows a stale or zero figure is worse than one that
            // shows nothing.
            if (!task.IsCompletedSuccessfully) continue;
            if (task.Result is { } capacity) measured.Add((mount, capacity));
        }
        return measured;
    }

    /// <summary>One mount's capacity, or null when it cannot be read.
    ///
    /// **This is the call that can block forever**, which is why it only ever runs inside a
    /// probe task. DriveInfo's properties are statfs() underneath, and statfs on a hung NFS
    /// mount is uninterruptible -- no CancellationToken exists that could end it, so the
    /// deadline is enforced by not waiting rather than by cancelling.</summary>
    private static Capacity? Stat(string mountPoint)
    {
        try
        {
            var drive = new DriveInfo(mountPoint);
            var total = drive.TotalSize;
            if (total <= 0) return null;

            // TotalFreeSpace, not AvailableFreeSpace. On ext4 the two differ by the 5%
            // root reserve, and "how full is this disk" is not "what may a non-root user
            // still write" -- reporting the latter shows every fresh disk as 5% used.
            return new Capacity(
                Math.Round(total / BytesPerGb, 1),
                Math.Round((total - drive.TotalFreeSpace) / BytesPerGb, 1));
        }
        catch { return null; }
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
