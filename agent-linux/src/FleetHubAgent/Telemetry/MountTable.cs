using System.Text;

namespace FleetHubAgent.Telemetry;

/// <summary>One line of /proc/mounts: what is mounted, where, and as what.</summary>
public readonly record struct MountEntry(string Device, string MountPoint, string FsType);

/// <summary>
/// Reads /proc/mounts, which is the only way to learn a mount's FILESYSTEM TYPE without
/// touching the mount itself.
///
/// **That is the whole reason this file exists.** VolumeReader used to start from
/// DriveInfo.GetDrives(), which statfs()es every mount point before the caller can filter
/// anything -- so a hung NFS or CIFS mount blocked the walk in an uninterruptible syscall and
/// the telemetry loop stalled at its five-second tick, on a machine that was otherwise
/// perfectly healthy. /proc/mounts is served by the kernel out of the mount table it already
/// holds: reading it never touches the remote server, so a dead file server is filtered out by
/// type before anything stats it. Roadmap #22 named this bug; this is its first half.
///
/// **Octal escapes are decoded, not ignored.** The kernel writes a mount point containing a
/// space as `\040`, a tab as `\011`, a newline as `\012` and a backslash as `\134`. A reader
/// that skipped this would hand `/mnt/my\040disk` to statfs, get ENOENT, and report the
/// machine as having one volume fewer than it has -- silently, and only on the machines whose
/// mount points a human named.
///
/// Parsing is separated from reading so a test can feed it the fixtures that matter (a snap
/// squashfs pile, a bind mount, an escaped mount point) without a container per case.
/// </summary>
public static class MountTable
{
    /// <summary>The kernel's own mount table. /proc/self/mounts rather than /proc/mounts:
    /// they are the same file on a host with one namespace, and in a container the former is
    /// the namespace this process actually sees -- which is the set whose free space is worth
    /// reporting.</summary>
    public const string MountsPath = "/proc/self/mounts";

    /// <summary>Every mount this process can see, in kernel order. Never throws: a machine
    /// whose /proc is not mounted (a chroot, an unusual container) reports no volumes rather
    /// than costing the caller its whole sensor block.</summary>
    public static IReadOnlyList<MountEntry> Read(string? path = null)
    {
        try { return Parse(File.ReadAllLines(path ?? MountsPath)); }
        catch { return Array.Empty<MountEntry>(); }
    }

    /// <summary>Parse mount-table lines. Malformed lines are skipped rather than raising --
    /// one unreadable entry must not cost a machine every volume after it.
    ///
    /// **Later entries win.** A second mount over the same directory hides the first, and
    /// what the kernel would answer a statfs with is the visible one; reporting the shadowed
    /// mount's free space would describe a filesystem nobody on this machine can write to.</summary>
    public static IReadOnlyList<MountEntry> Parse(IEnumerable<string> lines)
    {
        var byMountPoint = new Dictionary<string, MountEntry>(StringComparer.Ordinal);
        var order = new List<string>();

        foreach (var line in lines)
        {
            // device, mount point, fstype, options, dump, pass -- space-separated, and the
            // first three are all we can learn anything from.
            var parts = line.Split(' ', StringSplitOptions.RemoveEmptyEntries);
            if (parts.Length < 3) continue;

            var mountPoint = Unescape(parts[1]);
            if (mountPoint.Length == 0) continue;

            if (!byMountPoint.ContainsKey(mountPoint)) order.Add(mountPoint);
            byMountPoint[mountPoint] = new MountEntry(
                Unescape(parts[0]), mountPoint, Unescape(parts[2]));
        }

        return order.Select(m => byMountPoint[m]).ToArray();
    }

    /// <summary>Decode the kernel's octal escapes (\040 and friends). A backslash that does
    /// not begin a three-digit octal escape is a literal backslash, which is what a path
    /// written by something other than the kernel would contain.</summary>
    internal static string Unescape(string raw)
    {
        if (!raw.Contains('\\')) return raw;

        var sb = new StringBuilder(raw.Length);
        for (var i = 0; i < raw.Length; i++)
        {
            if (raw[i] != '\\' || i + 3 >= raw.Length)
            {
                sb.Append(raw[i]);
                continue;
            }

            var digits = raw.AsSpan(i + 1, 3);
            var value = 0;
            var ok = true;
            foreach (var c in digits)
            {
                if (c is < '0' or > '7') { ok = false; break; }
                value = (value * 8) + (c - '0');
            }

            if (!ok) { sb.Append(raw[i]); continue; }
            sb.Append((char)value);
            i += 3;
        }
        return sb.ToString();
    }
}
