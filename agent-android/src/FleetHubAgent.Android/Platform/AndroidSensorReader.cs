using System.Globalization;
using Android.App;
using Android.Content;
using Android.OS;
using Microsoft.Extensions.Logging;
using FleetHubAgent.Telemetry;

namespace FleetHubAgent.Android.Platform;

/// <summary>
/// Reads what an ordinary Android app is allowed to know about the device it is on, and
/// flattens it into the sensor shape the hub already understands.
///
/// **The shape is not negotiable and is not obvious.** app.py's extract_diagnostics finds a
/// value by its Type plus a substring of hardware_id, and its memory and volume readers match
/// on EXACT sensor names ("memory used", "total space") under specific identifiers ("/ram",
/// "/volume/..."). So this reader emits synthetic identifiers that look like the Windows
/// agent's rather than anything Android calls these things. Getting a name wrong here does not
/// error; it makes a card on the machine page stay empty forever. The Windows and Linux agents'
/// VolumeReader.cs is the reference for the volume half, and the two must agree because one hub
/// page renders both.
///
/// **The temperature story is the interesting one.** Every other agent reports a CPU
/// temperature, and the hub's fleet chart, its alert rules and its history are all built on
/// that one number. An ordinary Android app usually cannot read one: /sys/class/thermal is
/// blocked by SELinux on most modern devices, and HardwarePropertiesManager is system-only. So
/// this reader tries the thermal zones, and falls back to BATTERY temperature, which is
/// available on every device that has a battery and needs no permission at all.
///
/// That fallback is a real measurement, not a stand-in: a phone in a hot van, a tablet charging
/// in a sealed enclosure and a device with a failing cell are exactly the fleet-visible
/// problems a temperature chart should catch, and all three show up in battery temperature.
/// What it is NOT is a CPU temperature, so the sensor it emits says "Battery" and the machine
/// page shows it as such. What the hub charts as this machine's `temp` is whichever source was
/// used, and Read logs which one once, so nobody has to guess from the number.
/// </summary>
internal sealed class AndroidSensorReader : ISensorSource
{
    private const double BytesPerGb = 1024d * 1024d * 1024d;

    /// <summary>Thermal zone types that are worth reading as a CPU temperature. Matched as
    /// substrings of /sys/class/thermal/thermal_zoneN/type, whose values are vendor-defined
    /// and inconsistent ("cpu-0-0-usr", "soc_thermal", "mtktscpu"). An unmatched zone is
    /// ignored rather than averaged in: a modem or a charger zone reported as this machine's
    /// temperature would put a device at the top of the fleet's hottest list for a reading
    /// that is normal for that part.</summary>
    private static readonly string[] CpuZoneHints =
        { "cpu", "soc", "tsens_tz_sensor", "mtktscpu", "apollo", "big", "little" };

    private readonly Context _context;
    private readonly ILogger _log;
    private bool _sourceLogged;

    public AndroidSensorReader(Context context, ILogger log)
    {
        _context = context.ApplicationContext ?? context;
        _log = log;
    }

    /// <inheritdoc />
    public SensorSnapshot Read()
    {
        var sensors = new List<SensorReading>();
        double? reported = null;

        try
        {
            var cpuTemp = ReadCpuZoneTemp();
            if (cpuTemp is double cpu)
            {
                reported = cpu;
                sensors.Add(Reading("CPU", "/cpu/0", "Temperature", "CPU Package", cpu, "{0:0.0} °C"));
            }

            var battery = ReadBattery(sensors);
            // Only when no CPU zone answered. Both are emitted as sensors either way -- the
            // machine page should show what the device reports -- but exactly one of them can
            // be the number the hub charts.
            reported ??= battery;

            AppendMemory(sensors);
            AppendStorage(sensors);
            AppendCpuLoad(sensors);

            if (!_sourceLogged)
            {
                _sourceLogged = true;
                _log.LogInformation(
                    "Reporting {Source} as this machine's temperature. {Note}",
                    cpuTemp is not null ? "a CPU thermal zone" : "battery temperature",
                    cpuTemp is not null
                        ? ""
                        : "No readable CPU thermal zone on this device, which is normal -- " +
                          "SELinux blocks /sys/class/thermal on most modern Android.");
            }
        }
        catch (Exception e)
        {
            // Never throws: the telemetry loop is unattended and one unusual device must not
            // stop it reporting at all.
            _log.LogWarning("Sensor read failed: {Msg}", e.Message);
        }

        return new SensorSnapshot(reported, sensors);
    }

    // ------------------------------------------------------------------ temperature

    /// <summary>The hottest matching CPU thermal zone, or null when none can be read.
    ///
    /// The HOTTEST rather than the first or the mean, matching the Windows agent's choice for
    /// multi-die packages: a fleet console's temperature question is "is anything too hot",
    /// and averaging a hot cluster with an idle one answers a question nobody asked.
    ///
    /// Expect null on most devices. Reading /sys is not an error case here, it is the ordinary
    /// case, so a failure is silent and the caller falls back.</summary>
    private static double? ReadCpuZoneTemp()
    {
        double? hottest = null;
        try
        {
            var root = new DirectoryInfo("/sys/class/thermal");
            if (!root.Exists) return null;

            foreach (var zone in root.GetDirectories("thermal_zone*"))
            {
                string type;
                string raw;
                try
                {
                    type = File.ReadAllText(Path.Combine(zone.FullName, "type")).Trim().ToLowerInvariant();
                    if (!CpuZoneHints.Any(h => type.Contains(h, StringComparison.Ordinal))) continue;
                    raw = File.ReadAllText(Path.Combine(zone.FullName, "temp")).Trim();
                }
                catch { continue; }   // SELinux denial, or a zone that vanished

                if (!double.TryParse(raw, NumberStyles.Integer, CultureInfo.InvariantCulture, out var value))
                    continue;

                // Vendors report millidegrees, decidegrees or degrees from the same file, with
                // no unit anywhere. Scale by magnitude, then sanity-check -- a device reading
                // 0 or 250 °C is a misread unit or an unpopulated zone, not a fire.
                var celsius = Math.Abs(value) > 1000 ? value / 1000d
                            : Math.Abs(value) > 200 ? value / 10d
                            : value;
                if (celsius is <= 0 or > 150) continue;

                if (hottest is null || celsius > hottest) hottest = celsius;
            }
        }
        catch { return null; }

        return hottest;
    }

    /// <summary>Battery temperature, level and voltage from the sticky ACTION_BATTERY_CHANGED
    /// broadcast, appending each as a sensor and returning the temperature.
    ///
    /// A null receiver against a sticky broadcast returns the last value immediately without
    /// registering anything -- no callback, no lifecycle, nothing to unregister. That is why
    /// this reader has no subscription to leak.</summary>
    private double? ReadBattery(List<SensorReading> sensors)
    {
        Intent? status;
        try
        {
            status = _context.RegisterReceiver(
                (BroadcastReceiver?)null, new IntentFilter(Intent.ActionBatteryChanged));
        }
        catch { return null; }
        if (status is null) return null;

        double? temperature = null;

        // Tenths of a degree Celsius, per BatteryManager. -1 is "not reported".
        var rawTemp = status.GetIntExtra(BatteryManager.ExtraTemperature, int.MinValue);
        if (rawTemp != int.MinValue && rawTemp > 0)
        {
            var celsius = rawTemp / 10d;
            if (celsius is > 0 and <= 150)
            {
                temperature = celsius;
                sensors.Add(Reading("Battery", "/battery/0", "Temperature", "Battery", celsius, "{0:0.0} °C"));
            }
        }

        var level = status.GetIntExtra(BatteryManager.ExtraLevel, -1);
        var scale = status.GetIntExtra(BatteryManager.ExtraScale, -1);
        if (level >= 0 && scale > 0)
        {
            // "Level" as a Load sensor: the hub charts nothing from it, but the machine page
            // lists every sensor it is sent, and "this tablet has been on 4% since Monday" is
            // the single most useful thing a fleet console can say about a phone.
            var pct = Math.Round(level * 100d / scale, 1);
            sensors.Add(Reading("Battery", "/battery/0", "Load", "Charge Level", pct, "{0:0.0} %"));
        }

        var millivolts = status.GetIntExtra(BatteryManager.ExtraVoltage, -1);
        if (millivolts > 0)
            sensors.Add(Reading("Battery", "/battery/0", "Voltage", "Battery",
                millivolts / 1000d, "{0:0.000} V"));

        return temperature;
    }

    // ------------------------------------------------------------------ memory

    /// <summary>RAM, in the exact shape app.py's _memory_gb matches on.
    ///
    /// The names "Memory Used" and "Memory Available" and the "/ram" identifier are a contract
    /// with the hub, not a description -- _find_sensor_exact wants those two names under a
    /// hardware string containing "ram", and total is computed as used + available. A different
    /// spelling leaves the Memory chart permanently blank with no error anywhere.</summary>
    private void AppendMemory(List<SensorReading> sensors)
    {
        try
        {
            if (_context.GetSystemService(Context.ActivityService) is not ActivityManager am) return;

            var info = new ActivityManager.MemoryInfo();
            am.GetMemoryInfo(info);

            var totalGb = info.TotalMem / BytesPerGb;
            var availGb = info.AvailMem / BytesPerGb;
            if (totalGb <= 0) return;

            var usedGb = Math.Max(0, totalGb - availGb);

            sensors.Add(Reading("Generic Memory", "/ram", "Data", "Memory Used",
                Math.Round(usedGb, 1), "{0:0.0} GB"));
            sensors.Add(Reading("Generic Memory", "/ram", "Data", "Memory Available",
                Math.Round(availGb, 1), "{0:0.0} GB"));
            sensors.Add(Reading("Generic Memory", "/ram", "Load", "Memory",
                Math.Round(usedGb / totalGb * 100, 1), "{0:0.0} %"));
        }
        catch (Exception e)
        {
            _log.LogDebug("Memory read failed: {Msg}", e.Message);
        }
    }

    // ------------------------------------------------------------------ storage

    /// <summary>Per-volume capacity as the "/volume/..." Data sensors the hub's Storage cards
    /// are built from -- the same pair the Windows and Linux VolumeReader emit.
    ///
    /// **Which volumes count is much simpler here than on Linux and still not trivial.** An app
    /// cannot see the device's mount table, so there is nothing to filter: what it can stat is
    /// its own data directory (which lives on the /data partition, and is what "storage full"
    /// means to whoever holds the device) and whatever external volumes the system hands back.
    /// GetExternalFilesDirs is used rather than the deprecated ExternalStorageDirectory because
    /// it returns one entry per physical volume, which is how a removable SD card gets its own
    /// card in the console instead of being folded into internal storage.</summary>
    private void AppendStorage(List<SensorReading> sensors)
    {
        AppendVolume(sensors, global::Android.OS.Environment.DataDirectory?.AbsolutePath,
            "Internal storage", "/volume/internal");

        try
        {
            var externals = _context.GetExternalFilesDirs(null);
            if (externals is null) return;

            // Index 0 is the app's slice of internal storage, which is the same physical
            // volume already counted above -- adding it would double this device's storage in
            // the fleet totals, the same failure the Linux reader's bind-mount check avoids.
            for (var i = 1; i < externals.Length; i++)
            {
                var path = externals[i]?.AbsolutePath;
                if (string.IsNullOrEmpty(path)) continue;
                AppendVolume(sensors, path, $"External storage {i}", $"/volume/external-{i}");
            }
        }
        catch (Exception e)
        {
            _log.LogDebug("External storage read failed: {Msg}", e.Message);
        }
    }

    private void AppendVolume(List<SensorReading> sensors, string? path, string label, string id)
    {
        if (string.IsNullOrEmpty(path)) return;
        try
        {
            var stat = new StatFs(path);
            var total = (double)stat.BlockCountLong * stat.BlockSizeLong;
            if (total <= 0) return;

            // AvailableBlocksLong, not FreeBlocksLong: the difference is the reserve only a
            // privileged process may write into, and "how full is this device" is not "what
            // may root still write". Reporting free blocks shows every device as slightly
            // emptier than the user's own storage screen says, which is the number they will
            // compare against.
            var free = (double)stat.AvailableBlocksLong * stat.BlockSizeLong;

            var totalGb = Math.Round(total / BytesPerGb, 1);
            var usedGb = Math.Round((total - free) / BytesPerGb, 1);

            sensors.Add(Reading(label, id, "Data", "Total Space", totalGb, "{0:0.0} GB"));
            sensors.Add(Reading(label, id, "Data", "Used Space", usedGb, "{0:0.0} GB"));
        }
        catch (Exception e)
        {
            _log.LogDebug("Volume {Path} could not be read: {Msg}", path, e.Message);
        }
    }

    // ------------------------------------------------------------------ cpu load

    private long _lastCpuTotal;
    private long _lastCpuIdle;

    /// <summary>
    /// System-wide CPU load from /proc/stat, as a percentage since the previous read.
    ///
    /// **Best-effort and frequently unavailable, by design rather than by accident.** Many
    /// Android builds deny an app read access to /proc/stat under SELinux; some return a file
    /// scoped to the calling app. Both cases end here as "no sensor", which costs the machine
    /// page its CPU Load card and nothing else -- the temperature, memory and storage this
    /// agent exists to report are unaffected.
    ///
    /// The first call after start can only prime the counters (a load percentage needs two
    /// samples), so it emits nothing. At the sensor cadence that is one missing reading, once.
    /// </summary>
    private void AppendCpuLoad(List<SensorReading> sensors)
    {
        try
        {
            var line = File.ReadLines("/proc/stat").FirstOrDefault();
            if (line is null || !line.StartsWith("cpu ", StringComparison.Ordinal)) return;

            var fields = line.Split(' ', StringSplitOptions.RemoveEmptyEntries);
            // user nice system idle iowait irq softirq ...
            if (fields.Length < 5) return;

            long total = 0;
            for (var i = 1; i < fields.Length; i++)
            {
                if (!long.TryParse(fields[i], NumberStyles.Integer, CultureInfo.InvariantCulture, out var v))
                    return;
                total += v;
            }
            if (!long.TryParse(fields[4], NumberStyles.Integer, CultureInfo.InvariantCulture, out var idle))
                return;

            var totalDelta = total - _lastCpuTotal;
            var idleDelta = idle - _lastCpuIdle;
            _lastCpuTotal = total;
            _lastCpuIdle = idle;

            // First sample, or counters that went backwards (a reboot between reads).
            if (totalDelta <= 0 || idleDelta < 0) return;

            var busyPct = Math.Round((totalDelta - idleDelta) * 100d / totalDelta, 1);
            busyPct = Math.Clamp(busyPct, 0, 100);

            // "CPU Total" is the name app.py's extract_diagnostics prefers for cpu_load_pct.
            sensors.Add(Reading("CPU", "/cpu/0", "Load", "CPU Total", busyPct, "{0:0.0} %"));
        }
        catch
        {
            // Denied, absent, or unparseable. The ordinary case on modern Android.
        }
    }

    // ------------------------------------------------------------------ helpers

    private static SensorReading Reading(
        string hardware, string hardwareId, string type, string name, double value, string format) =>
        new()
        {
            Hardware = hardware,
            HardwareId = hardwareId,
            Group = type,
            Name = name,
            Type = type,
            Value = value,
            Text = string.Format(CultureInfo.InvariantCulture, format, value),
        };

    /// <summary>Nothing to release: the battery read uses a sticky broadcast with a null
    /// receiver, so this reader never registers anything. Present because ISensorSource
    /// requires it, and the other agents' readers genuinely do hold handles.</summary>
    public void Dispose() { }
}
