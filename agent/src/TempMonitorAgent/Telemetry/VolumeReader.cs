namespace TempMonitorAgent.Telemetry;

/// <summary>
/// Per-volume capacity, appended to the ordinary sensor block as synthetic readings.
///
/// LibreHardwareMonitor reports storage "Used Space" as a PERCENTAGE and nothing else --
/// the absolute size never leaves its report string, so no sensor carries it. A helpdesk
/// operator looking at a machine wants "C: 412 / 476 GB", not "86%", and that number has
/// to come from somewhere. It comes from here: DriveInfo, which is exactly what LHM reads
/// internally to compute its own percentage.
///
/// These ride in the existing `sensors` list rather than a new /api/report field on
/// purpose. The hub already stores, caches and forwards that list, an older hub ignores
/// unfamiliar entries harmlessly, and the payload stays one shape for both the agent and
/// companion.py.
///
/// The identifier prefix "/volume/" is the contract with the hub (see app.py's
/// _disk_volumes): nothing LHM emits starts with it, so the hub can tell our synthetic
/// readings from real hardware without guessing.
/// </summary>
public static class VolumeReader
{
    private const double BytesPerGb = 1024d * 1024d * 1024d;

    /// <summary>Appends two Data sensors ("Total Space", "Used Space", both GB) per fixed,
    /// ready volume. Never throws: a volume that disappears mid-enumeration, or one the
    /// service can't stat, is skipped rather than costing us the whole sensor block.</summary>
    public static void Append(List<SensorReading> sensors)
    {
        DriveInfo[] drives;
        try { drives = DriveInfo.GetDrives(); }
        catch { return; }

        foreach (var drive in drives)
        {
            double totalGb, usedGb;
            string hardware, hardwareId;
            try
            {
                // Fixed only. A mapped network share's free space is not this machine's
                // storage, and removable media comes and goes -- charting either as "this
                // PC's disk" is noise.
                if (drive.DriveType != DriveType.Fixed || !drive.IsReady) continue;

                var total = drive.TotalSize;
                if (total <= 0) continue;
                // TotalFreeSpace, not AvailableFreeSpace: with disk quotas in play the
                // latter is what THIS user may still write, which is not what "how full is
                // the disk" means.
                totalGb = Math.Round(total / BytesPerGb, 1);
                usedGb = Math.Round((total - drive.TotalFreeSpace) / BytesPerGb, 1);

                var root = drive.Name.TrimEnd('\\');          // "C:\" -> "C:"
                var label = SafeLabel(drive);
                hardware = string.IsNullOrEmpty(label) ? root : $"{root} ({label})";
                hardwareId = $"/volume/{root.TrimEnd(':').ToLowerInvariant()}";
            }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException)
            {
                continue;
            }

            sensors.Add(Reading(hardware, hardwareId, "Total Space", totalGb));
            sensors.Add(Reading(hardware, hardwareId, "Used Space", usedGb));
        }
    }

    private static string SafeLabel(DriveInfo drive)
    {
        try { return drive.VolumeLabel ?? ""; } catch { return ""; }
    }

    // Group/Type "Data" in GB, matching how SensorReader classifies LHM's own Data
    // sensors -- the hub's readers key off Type, so these have to look native.
    private static SensorReading Reading(string hardware, string hardwareId, string name, double gb) =>
        new()
        {
            Hardware = hardware,
            HardwareId = hardwareId,
            Group = "Data",
            Name = name,
            Type = "Data",
            Value = gb,
            Text = $"{gb.ToString("0.0", System.Globalization.CultureInfo.InvariantCulture)} GB",
        };
}
