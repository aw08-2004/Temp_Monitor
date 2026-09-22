using FleetHubAgent.Telemetry;

namespace FleetHubAgent.Tests;

/// <summary>
/// Covers the volume walk's SELECTION, which is the half that decides what an operator sees
/// on the Storage card and the half that used to hang the telemetry loop.
///
/// The silent failure this file exists to catch is the one that was live until roadmap #22's
/// fix: a machine with a dead NFS mount reported no readings at all, for as long as the file
/// server stayed down, while looking perfectly online in the console. Nothing about that is
/// visible from the hub, so the guard has to be here -- that a network filesystem never
/// reaches the stat at all, because it is filtered out by TYPE from a mount table that costs
/// nothing to read.
///
/// The second failure is quieter still: a snap-using machine listing forty squashfs volumes,
/// each 100% full by design, parks itself permanently at the top of the fleet's "least free
/// disk" list and nothing anybody does at that machine can move it.
/// </summary>
public class MountWalkTests
{
    private static IReadOnlyList<MountEntry> Parse(params string[] lines) =>
        MountTable.Parse(lines);

    [Fact]
    public void Parses_the_three_fields_it_uses()
    {
        var mounts = Parse("/dev/sda2 / ext4 rw,relatime 0 0");
        var mount = Assert.Single(mounts);
        Assert.Equal("/dev/sda2", mount.Device);
        Assert.Equal("/", mount.MountPoint);
        Assert.Equal("ext4", mount.FsType);
    }

    [Fact]
    public void Decodes_the_kernel_octal_escapes()
    {
        // A mount point a human named. Handing "/mnt/my\040disk" to statfs is ENOENT, and the
        // machine silently reports one volume fewer than it has.
        var mount = Assert.Single(Parse(@"/dev/sdb1 /mnt/my\040disk ext4 rw 0 0"));
        Assert.Equal("/mnt/my disk", mount.MountPoint);
    }

    [Fact]
    public void Leaves_a_lone_backslash_alone()
    {
        var mount = Assert.Single(Parse(@"/dev/sdb1 /mnt/a\b ext4 rw 0 0"));
        Assert.Equal(@"/mnt/a\b", mount.MountPoint);
    }

    [Fact]
    public void A_later_mount_over_the_same_point_wins()
    {
        // What a statfs would answer is the visible mount, not the one hidden under it.
        var mounts = Parse(
            "/dev/sda2 /data ext4 rw 0 0",
            "/dev/sdb1 /data xfs rw 0 0");
        var mount = Assert.Single(mounts);
        Assert.Equal("xfs", mount.FsType);
        Assert.Equal("/dev/sdb1", mount.Device);
    }

    [Fact]
    public void Skips_lines_it_cannot_read_without_losing_the_rest()
    {
        var mounts = Parse("garbage", "", "/dev/sda2 / ext4 rw 0 0");
        Assert.Single(mounts);
    }

    [Fact]
    public void A_hung_network_mount_is_never_a_candidate()
    {
        // THE regression guard for roadmap #22's telemetry stall. nfs and cifs are not in the
        // allow-list, so a dead file server is rejected from the mount table -- before
        // anything stats it, which is the only point at which rejecting it is free.
        var candidates = VolumeReader.Candidates(Parse(
            "/dev/sda2 / ext4 rw 0 0",
            "fileserver:/export /mnt/nfs nfs4 rw 0 0",
            @"//fileserver/share /mnt/smb cifs rw 0 0"));

        Assert.Equal(new[] { "/" }, candidates.Select(c => c.MountPoint));
    }

    [Fact]
    public void Pseudo_filesystems_and_snaps_are_left_out()
    {
        var candidates = VolumeReader.Candidates(Parse(
            "proc /proc proc rw 0 0",
            "sysfs /sys sysfs rw 0 0",
            "tmpfs /run tmpfs rw 0 0",
            "/dev/loop3 /snap/core22/1122 squashfs ro 0 0",
            "/dev/loop4 /snap/firefox/4173 squashfs ro 0 0",
            "/dev/sda2 / ext4 rw 0 0",
            "/dev/sda1 /boot/efi vfat rw 0 0"));

        Assert.Equal(new[] { "/", "/boot/efi" }, candidates.Select(c => c.MountPoint));
    }

    [Fact]
    public void One_device_is_counted_once()
    {
        // A bind mount and a btrfs subvolume both show the same device twice. Counting a 2 TB
        // disk twice doubles this machine's storage in the fleet totals, and the first mount
        // point in kernel order -- "/" rather than a bind of a directory inside it -- is the
        // one worth reporting.
        var candidates = VolumeReader.Candidates(Parse(
            "/dev/sda2 / btrfs rw,subvol=@ 0 0",
            "/dev/sda2 /home btrfs rw,subvol=@home 0 0",
            "/dev/sda2 /var/lib/docker/btrfs btrfs rw 0 0"));

        var kept = Assert.Single(candidates);
        Assert.Equal("/", kept.MountPoint);
    }

    [Fact]
    public void A_missing_mount_table_costs_no_readings_and_no_exception()
    {
        // A chroot or an unusual container has no /proc. The sensor block must survive it.
        Assert.Empty(MountTable.Read("/nonexistent/proc/mounts"));

        var sensors = new List<SensorReading>();
        VolumeReader.Append(sensors);   // the real /proc on the test host, or nothing at all
        Assert.All(sensors, s => Assert.StartsWith("/volume/", s.HardwareId));
    }
}
