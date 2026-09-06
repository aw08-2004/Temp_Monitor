using System.Globalization;
using Microsoft.Extensions.Logging;

namespace FleetHubAgent.Telemetry;

/// <summary>
/// Reads this machine's sensors out of /sys and /proc, and flattens them into the shape the
/// hub already understands.
///
/// **The hub's readers are the specification here, not the kernel's.** app.py's
/// _find_sensor_value builds a haystack out of `hardware_id` plus `hardware` and looks for the
/// literal substrings "cpu", "gpu" and "ram" in it, then matches `type` exactly and `name` by
/// a preference list. That was written against LibreHardwareMonitor's identifiers on Windows
/// ("/amdcpu/0", "/ram"), and it is a wire contract like any other -- so this reader emits
/// those SYNTHETIC identifiers rather than the hwmon chip names it actually read. A sensor
/// block that is faithful to /sys and unrecognised by the hub would leave every Linux
/// machine's overview page empty, which is the silent failure this class exists to avoid.
///
/// The mapping, and what each one populates in the console:
///
///   /cpu/0  Temperature  "&lt;label&gt;"          -> the machine's temp, charts, fleet "hottest"
///   /cpu/0  Load         "CPU Total"          -> cpu_load_pct
///   /cpu/0  Clock        "Core Average"       -> cpu_clock_mhz
///   /ram    Load         "Memory"             -> memory_load_pct
///   /ram    Data         "Memory Used"        -> mem_used_gb   (exact name; see _memory_gb)
///   /ram    Data         "Memory Available"   -> mem_total_gb = used + available
///
/// Everything else a hwmon chip offers (NVMe, wifi, chipset, battery) is still reported, under
/// its own "/hwmon/&lt;chip&gt;" identifier. It populates nothing on the machine page today and
/// costs nothing to send, and it is what a later reader would be built from rather than
/// re-discovered.
///
/// GPU and fan sensors are deliberately absent. Both are readable in principle -- amdgpu
/// exposes them through hwmon, NVIDIA only through nvidia-smi -- but a half-populated GPU card
/// that is right on AMD and empty on NVIDIA is worse than no card, and the hub hides hardware
/// it has never seen. See ROADMAP.MD #22.
/// </summary>
public sealed class ProcSensorReader : ISensorSource
{
    private const string HwmonRoot = "/sys/class/hwmon";
    private const double BytesPerGb = 1024d * 1024d * 1024d;

    /// <summary>hwmon chip names that ARE the CPU's own thermal sensor, in preference order.
    ///
    /// acpitz is last and is a fallback only. It is the ACPI thermal zone, which on a desktop
    /// with coretemp present is usually a different, laggier sensor a metre from the die --
    /// but on a laptop or a VM it is frequently the only temperature there is, and reporting
    /// no temperature at all makes a machine invisible to the fleet's "hottest" list and to
    /// every temperature rule.</summary>
    private static readonly string[] CpuChips =
        { "coretemp", "k10temp", "k8temp", "zenpower", "cpu_thermal", "soc_thermal", "acpitz" };

    private readonly ILogger<ProcSensorReader> _log;

    /// <summary>Previous /proc/stat totals, for the CPU load delta. See ReadCpuLoad.</summary>
    private (ulong Idle, ulong Total)? _lastCpuTimes;

    private bool _loggedNoTemp;

    public ProcSensorReader(ILogger<ProcSensorReader> log) => _log = log;

    public SensorSnapshot Read()
    {
        var sensors = new List<SensorReading>();
        double? cpuTemp = null;

        // Each block is guarded separately, on purpose. A machine with an unreadable hwmon
        // tree should still report its memory and its disks; one try/catch around the lot
        // would trade the whole sensor block for the weakest reader in it.
        try { cpuTemp = ReadTemperatures(sensors); }
        catch (Exception e) { _log.LogDebug("Temperature read failed: {Msg}", e.Message); }

        // Only when hwmon gave us no CPU temperature, never alongside it.
        //
        // **This is what makes a VM visible at all.** The agent does not report to /api/report
        // without a temperature (see Worker), because the endpoint requires a number and 0 °C
        // would be a lie that drags the fleet average down and puts the machine at the top of
        // the "coldest" list. A VM under KVM or VMware usually has an empty /sys/class/hwmon
        // but still exposes an ACPI thermal zone, so without this a virtual machine would
        // never report its OS, model, disks or memory -- it would simply not appear.
        //
        // Second rather than merged because the two trees overlap: on a physical Intel box
        // coretemp and x86_pkg_temp are the same die sensor read twice, and reporting both
        // would show an operator two temperatures for one CPU that disagree by a degree.
        if (cpuTemp is null)
        {
            try { cpuTemp = ReadThermalZones(sensors); }
            catch (Exception e) { _log.LogDebug("Thermal zone read failed: {Msg}", e.Message); }
        }

        try { ReadCpuLoad(sensors); }
        catch (Exception e) { _log.LogDebug("CPU load read failed: {Msg}", e.Message); }

        try { ReadCpuClock(sensors); }
        catch (Exception e) { _log.LogDebug("CPU clock read failed: {Msg}", e.Message); }

        try { ReadMemory(sensors); }
        catch (Exception e) { _log.LogDebug("Memory read failed: {Msg}", e.Message); }

        try { VolumeReader.Append(sensors); }
        catch (Exception e) { _log.LogDebug("Volume read failed: {Msg}", e.Message); }

        if (cpuTemp is null && !_loggedNoTemp)
        {
            // Once, not every ten seconds. A VM genuinely has no thermal sensor, and this is
            // the difference between "explained" and "noise an operator learns to ignore".
            _loggedNoTemp = true;
            _log.LogInformation(
                "No CPU temperature source found under {Root}. This is normal in a VM; on " +
                "physical hardware it usually means the chip's driver (coretemp, k10temp) is " +
                "not loaded.", HwmonRoot);
        }

        return new SensorSnapshot(cpuTemp, sensors);
    }

    // ---------------------------------------------------------------- temperatures

    /// <summary>Walk every hwmon chip, emit every temp*_input it offers, and return the best
    /// CPU temperature among them.</summary>
    private double? ReadTemperatures(List<SensorReading> sensors)
    {
        if (!Directory.Exists(HwmonRoot)) return null;

        var cpuCandidates = new List<(int Rank, string Label, double Value)>();

        foreach (var chipDir in Directory.EnumerateDirectories(HwmonRoot))
        {
            var chip = ReadTrimmed(Path.Combine(chipDir, "name")) ?? "hwmon";
            var rank = Array.IndexOf(CpuChips, chip);
            var isCpu = rank >= 0;

            // "/cpu/0" only for a chip we believe IS the CPU -- see the class note. Anything
            // else must NOT carry "cpu" in its identifier or the hub would average an NVMe
            // drive's temperature into the machine's.
            var hardwareId = isCpu ? "/cpu/0" : "/hwmon/" + chip;

            foreach (var inputPath in SafeFiles(chipDir, "temp*_input"))
            {
                var milli = ReadDouble(inputPath);
                if (milli is null) continue;
                var celsius = Math.Round(milli.Value / 1000d, 1);

                // 0 is what a sensor reports when it could not be read, and a 0 °C machine
                // would be the coldest and healthiest-looking box in the fleet. Negative is
                // the same class of nonsense. This mirrors the rule the hub applies in
                // _cpu_temp_candidates, applied here so the bad reading never ships.
                if (celsius <= 0 || celsius > 150) continue;

                var label = ReadTrimmed(inputPath.Replace("_input", "_label"))
                            ?? $"{chip} {Path.GetFileName(inputPath)}";

                sensors.Add(new SensorReading
                {
                    Hardware = isCpu ? $"CPU ({chip})" : chip,
                    HardwareId = hardwareId,
                    Group = "Temperature",
                    Name = label,
                    Type = "Temperature",
                    Value = celsius,
                    Text = celsius.ToString("0.0", CultureInfo.InvariantCulture) + " °C",
                });

                if (isCpu) cpuCandidates.Add((rank, label, celsius));
            }
        }

        if (cpuCandidates.Count == 0) return null;

        // Prefer a package-wide reading over a single core, then the better chip, then the
        // hottest. A "Package id 0"/"Tctl" sensor is the number a vendor's own tooling shows;
        // picking core #3 because it happened to sort first would make two identical machines
        // disagree for no reason anyone could see.
        return cpuCandidates
            .OrderBy(c => IsPackageLabel(c.Label) ? 0 : 1)
            .ThenBy(c => c.Rank)
            .ThenByDescending(c => c.Value)
            .First().Value;
    }

    // ---------------------------------------------------------------- thermal zones

    /// <summary>
    /// The ACPI/SoC thermal zones, used only when hwmon offered no CPU temperature.
    ///
    /// /sys/class/thermal/thermal_zone*/ carries a `type` naming what the zone measures and a
    /// `temp` in millidegrees, the same unit as hwmon. The type strings are far less
    /// standardised than hwmon chip names -- "x86_pkg_temp" on Intel, "cpu-thermal" on ARM
    /// boards, "acpitz" on most VMs, and vendor strings like "INT3400 Thermal" on laptops --
    /// so anything that is not obviously NOT a CPU is accepted here. That is the right bias
    /// for a fallback: the alternative is no temperature at all, and a zone that turns out to
    /// measure the chassis is still a truer number than nothing.
    /// </summary>
    private static double? ReadThermalZones(List<SensorReading> sensors)
    {
        const string root = "/sys/class/thermal";
        if (!Directory.Exists(root)) return null;

        double? best = null;
        foreach (var zoneDir in Directory.EnumerateDirectories(root, "thermal_zone*"))
        {
            var milli = ReadDouble(Path.Combine(zoneDir, "temp"));
            if (milli is null) continue;
            var celsius = Math.Round(milli.Value / 1000d, 1);
            if (celsius <= 0 || celsius > 150) continue;

            var type = ReadTrimmed(Path.Combine(zoneDir, "type")) ?? Path.GetFileName(zoneDir);

            sensors.Add(new SensorReading
            {
                Hardware = $"CPU ({type})",
                HardwareId = "/cpu/0",
                Group = "Temperature",
                Name = type,
                Type = "Temperature",
                Value = celsius,
                Text = celsius.ToString("0.0", CultureInfo.InvariantCulture) + " °C",
            });

            // The hottest zone, not the first. Zone ordering is arbitrary and differs between
            // two machines off the same image, and the hottest is the one that matters for a
            // temperature rule.
            if (best is null || celsius > best) best = celsius;
        }
        return best;
    }

    /// <summary>Does this label name a whole-package sensor rather than one core?
    /// Internal so a test can hold the list -- which label wins decides the number the whole
    /// fleet is ranked by.</summary>
    internal static bool IsPackageLabel(string? label)
    {
        var text = (label ?? "").ToLowerInvariant();
        return text.Contains("package") || text.Contains("tctl") || text.Contains("tdie")
            || text.Contains("composite");
    }

    // ---------------------------------------------------------------- cpu load

    /// <summary>
    /// CPU utilisation from /proc/stat, as a delta between this read and the last.
    ///
    /// **The first read of the process reports nothing**, and that is correct rather than a
    /// gap to paper over: /proc/stat holds cumulative jiffies since boot, so a single sample
    /// yields average load since the machine started -- a number that is nearly constant, near
    /// meaningless, and indistinguishable on a chart from a real one. The hub omits a metric
    /// it was not sent; it has no way to un-believe one it was.
    /// </summary>
    private void ReadCpuLoad(List<SensorReading> sensors)
    {
        var line = File.ReadLines("/proc/stat").FirstOrDefault();
        if (line is null || !line.StartsWith("cpu ", StringComparison.Ordinal)) return;

        // user nice system idle iowait irq softirq steal guest guest_nice
        var fields = line.Split(' ', StringSplitOptions.RemoveEmptyEntries).Skip(1)
            .Select(f => ulong.TryParse(f, out var v) ? v : 0UL).ToArray();
        if (fields.Length < 5) return;

        var idle = fields[3] + fields[4];              // idle + iowait
        var total = fields.Aggregate(0UL, (a, b) => a + b);

        var previous = _lastCpuTimes;
        _lastCpuTimes = (idle, total);
        if (previous is null) return;

        var totalDelta = total - previous.Value.Total;
        var idleDelta = idle - previous.Value.Idle;
        // A zero delta means two reads landed inside one jiffy. Dividing by it is the obvious
        // crash; reporting 100% busy is the subtle one.
        if (totalDelta == 0) return;

        var load = Math.Clamp(Math.Round((1d - (double)idleDelta / totalDelta) * 100d, 1), 0, 100);

        sensors.Add(new SensorReading
        {
            Hardware = "CPU",
            HardwareId = "/cpu/0",
            Group = "Load",
            Name = "CPU Total",
            Type = "Load",
            Value = load,
            Text = load.ToString("0.0", CultureInfo.InvariantCulture) + " %",
        });
    }

    // ---------------------------------------------------------------- cpu clock

    /// <summary>Average current core frequency, in MHz.
    ///
    /// From /proc/cpuinfo's "cpu MHz" lines, which x86 kernels carry and most ARM ones do not
    /// -- absent means no Clock sensor, and the console hides the panel. Averaged across cores
    /// because that is what the hub's preference list asks for ("core average").</summary>
    private static void ReadCpuClock(List<SensorReading> sensors)
    {
        var speeds = new List<double>();
        foreach (var line in File.ReadLines("/proc/cpuinfo"))
        {
            if (!line.StartsWith("cpu MHz", StringComparison.Ordinal)) continue;
            var idx = line.IndexOf(':');
            if (idx < 0) continue;
            if (double.TryParse(line[(idx + 1)..].Trim(), NumberStyles.Float,
                                CultureInfo.InvariantCulture, out var mhz))
                speeds.Add(mhz);
        }
        if (speeds.Count == 0) return;

        var average = Math.Round(speeds.Average(), 0);
        sensors.Add(new SensorReading
        {
            Hardware = "CPU",
            HardwareId = "/cpu/0",
            Group = "Clock",
            Name = "Core Average",
            Type = "Clock",
            Value = average,
            Text = average.ToString("0", CultureInfo.InvariantCulture) + " MHz",
        });
    }

    // ---------------------------------------------------------------- memory

    /// <summary>
    /// Physical RAM, from /proc/meminfo.
    ///
    /// **MemAvailable, not MemFree.** MemFree excludes the page cache, so a healthy Linux box
    /// that has been up a week reports a few hundred megabytes free and would show as
    /// permanently at 95% memory pressure on every chart -- the single most common way a
    /// Linux machine gets misread by tooling written for Windows. MemAvailable is the kernel's
    /// own estimate of what a new allocation could actually get, which is the question the
    /// console's Memory panel is asking.
    ///
    /// The names are the hub's exact ones ("Memory Used", "Memory Available"): _memory_gb
    /// matches them exactly, not as substrings, and derives total as used + available.
    /// </summary>
    private static void ReadMemory(List<SensorReading> sensors)
    {
        double? totalKb = null, availableKb = null;
        foreach (var line in File.ReadLines("/proc/meminfo"))
        {
            if (line.StartsWith("MemTotal:", StringComparison.Ordinal)) totalKb = MemValue(line);
            else if (line.StartsWith("MemAvailable:", StringComparison.Ordinal)) availableKb = MemValue(line);
            if (totalKb is not null && availableKb is not null) break;
        }
        if (totalKb is null or <= 0 || availableKb is null) return;

        var totalGb = Math.Round(totalKb.Value * 1024d / BytesPerGb, 1);
        var availableGb = Math.Round(availableKb.Value * 1024d / BytesPerGb, 1);
        var usedGb = Math.Round(totalGb - availableGb, 1);
        var loadPct = Math.Clamp(
            Math.Round((totalKb.Value - availableKb.Value) / totalKb.Value * 100d, 1), 0, 100);

        sensors.Add(Ram("Load", "Memory", loadPct, loadPct.ToString("0.0", CultureInfo.InvariantCulture) + " %"));
        sensors.Add(Ram("Data", "Memory Used", usedGb, Gb(usedGb)));
        sensors.Add(Ram("Data", "Memory Available", availableGb, Gb(availableGb)));
    }

    /// <summary>"MemTotal:  16316420 kB" -> 16316420.</summary>
    private static double? MemValue(string line)
    {
        var parts = line.Split(' ', StringSplitOptions.RemoveEmptyEntries);
        return parts.Length >= 2 && double.TryParse(parts[1], NumberStyles.Float,
                                                    CultureInfo.InvariantCulture, out var v)
            ? v : null;
    }

    private static SensorReading Ram(string type, string name, double value, string text) => new()
    {
        Hardware = "Generic Memory",
        HardwareId = "/ram",
        Group = type,
        Name = name,
        Type = type,
        Value = value,
        Text = text,
    };

    private static string Gb(double gb) =>
        gb.ToString("0.0", CultureInfo.InvariantCulture) + " GB";

    // ---------------------------------------------------------------- /sys helpers
    //
    // Everything under /sys is a kernel file that can vanish between the enumeration and the
    // read (a USB thermal device unplugged mid-walk) or refuse the read outright. These three
    // swallow that rather than making every call site handle it -- an unreadable sensor is a
    // missing sensor, never an exception.

    private static IEnumerable<string> SafeFiles(string dir, string pattern)
    {
        try { return Directory.EnumerateFiles(dir, pattern); }
        catch { return Array.Empty<string>(); }
    }

    private static string? ReadTrimmed(string path)
    {
        try { return File.Exists(path) ? File.ReadAllText(path).Trim() : null; }
        catch { return null; }
    }

    private static double? ReadDouble(string path)
    {
        var text = ReadTrimmed(path);
        return double.TryParse(text, NumberStyles.Float, CultureInfo.InvariantCulture, out var v)
            ? v : null;
    }

    /// <summary>Nothing to release -- there is no handle, no driver and no service behind this
    /// reader, only files. Present because ISensorSource is IDisposable for the Windows
    /// implementation's sake, and dropping that from the interface would be a change to the
    /// shared shape for the benefit of the simpler side.</summary>
    public void Dispose() { }
}
