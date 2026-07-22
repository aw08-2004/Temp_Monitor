using LibreHardwareMonitor.Hardware;
using Microsoft.Extensions.Logging;
using TempMonitorAgent.State;

namespace TempMonitorAgent.Telemetry;

/// <summary>
/// Reads sensors in-process via LibreHardwareMonitorLib (no separate
/// LibreHardwareMonitor.exe / :8085 web server). Requires the PawnIO kernel driver
/// (installed by the installer) and runs fine under a SYSTEM service. Faithfully
/// mirrors companion.py's flatten_sensors + _walk/pick_cpu_temp selection.
/// </summary>
public sealed class SensorReader : ISensorSource
{
    private readonly ILogger<SensorReader> _log;
    private readonly Computer _computer;
    private readonly UpdateVisitor _visitor = new();
    private bool _opened;

    public SensorReader(ILogger<SensorReader> log)
    {
        _log = log;
        _computer = new Computer
        {
            IsCpuEnabled = true,
            IsGpuEnabled = true,
            IsMemoryEnabled = true,
            IsMotherboardEnabled = true,
            IsStorageEnabled = true,
            // Network throughput (in/out B/s) for the History dashboard. Whether it is
            // actually reported is gated per-read by RuntimeConfig.CollectNetwork so the hub's
            // metrics.collect_network toggle takes effect without a restart; the category stays
            // enabled here so toggling it back on is instant.
            IsNetworkEnabled = true,
        };
    }

    private void EnsureOpen()
    {
        if (_opened) return;
        _computer.Open();
        _opened = true;
    }

    public SensorSnapshot Read()
    {
        var sensors = new List<SensorReading>();
        var cpuTemps = new List<(string name, double value)>();
        // Read the toggle once per pass, fresh from the store (same discipline as PickCpuTemp)
        // so a hub config push takes effect on the next loop tick.
        var collectNetwork = RuntimeConfigStore.Current.CollectNetwork;

        try
        {
            EnsureOpen();
            _computer.Accept(_visitor); // updates every hardware + sub-hardware

            foreach (var hw in _computer.Hardware)
                CollectHardware(hw, sensors, cpuTemps, collectNetwork);

            // Per-volume capacity in GB, which LHM does not expose (it reports used space
            // only as a percentage). Appended after the hardware walk so real hardware keeps
            // its position in the block -- the hub's disk_load_pct takes the FIRST Load
            // sensor named "Used Space" and must keep meaning the physical drive.
            VolumeReader.Append(sensors);
        }
        catch (Exception e)
        {
            _log.LogWarning(e, "Sensor read failed");
            return new SensorSnapshot(null, sensors);
        }

        return new SensorSnapshot(PickCpuTemp(cpuTemps), sensors);
    }

    private void CollectHardware(IHardware hw, List<SensorReading> sensors,
                                 List<(string, double)> cpuTemps, bool collectNetwork)
    {
        // Honor the hub's network collection toggle: when off, skip the NIC category entirely
        // ("what sensor should be read"). Only network hardware is gated -- everything else is
        // always collected.
        if (hw.HardwareType == HardwareType.Network && !collectNetwork)
            return;

        var hardwareName = hw.Name;
        var hardwareId = hw.Identifier?.ToString()?.ToLowerInvariant();
        var isCpu = hardwareId is not null && hardwareId.Contains("cpu");

        foreach (var s in hw.Sensors)
        {
            double? value = LenientValue(s.Value);
            var name = s.Name ?? "";
            var typeStr = s.SensorType.ToString();

            sensors.Add(new SensorReading
            {
                Hardware = hardwareName,
                HardwareId = hardwareId,
                Group = GroupFor(s.SensorType),
                Name = name,
                Type = typeStr,
                Value = value,
                Text = FormatText(value, s.SensorType),
            });

            // Strict rule for CPU-temperature selection: 0/negative = "no reading".
            if (isCpu && s.SensorType == SensorType.Temperature &&
                value is double v && v > 0)
            {
                cpuTemps.Add((name.ToLowerInvariant(), v));
            }
        }

        foreach (var sub in hw.SubHardware)
            CollectHardware(sub, sensors, cpuTemps, collectNetwork);
    }

    /// <summary>
    /// Pick the primary CPU temperature, honouring the operator's configured preference.
    ///
    /// Read fresh from RuntimeConfigStore on every call rather than captured once, so a
    /// config push takes effect on the very next loop tick — no restart, no reconnect.
    /// </summary>
    private static double? PickCpuTemp(List<(string name, double value)> cpuTemps)
    {
        if (cpuTemps.Count == 0) return null;
        var config = RuntimeConfigStore.Current;

        // A pinned sensor is matched exactly: the operator chose it from the names this
        // machine actually reports, so a partial hit means the sensor is gone, and we
        // should fall through to the preference order rather than guess at a near-miss.
        if (!string.IsNullOrWhiteSpace(config.PrimarySensorName))
        {
            var want = config.PrimarySensorName!.Trim().ToLowerInvariant();
            foreach (var (name, value) in cpuTemps)
                if (name == want)
                    return value;
        }

        foreach (var wanted in config.PreferredSensors)
            foreach (var (name, value) in cpuTemps)
                if (name.Contains(wanted))
                    return value;

        // Any CPU temp beats none. Note this differs from the hub's re-derivation, which
        // deliberately returns "no opinion" instead — the hub can fall back to the value
        // we chose here, whereas we have nothing better to fall back to.
        return cpuTemps[0].value;
    }

    // Lenient parse for the general list: keep legitimate 0/negative, reject only
    // NaN/missing (companion.py _parse_sensor_value).
    private static double? LenientValue(float? raw)
    {
        if (raw is not float f || float.IsNaN(f)) return null;
        return Math.Round((double)f, 1);
    }

    private static string GroupFor(SensorType t) => t switch
    {
        SensorType.Voltage => "Voltages",
        SensorType.Current => "Currents",
        SensorType.Power => "Powers",
        SensorType.Clock => "Clocks",
        SensorType.Temperature => "Temperatures",
        SensorType.Load => "Load",
        SensorType.Frequency => "Frequencies",
        SensorType.Fan => "Fans",
        SensorType.Flow => "Flows",
        SensorType.Control => "Controls",
        SensorType.Level => "Levels",
        SensorType.Factor => "Factors",
        SensorType.Data => "Data",
        SensorType.SmallData => "Data",
        SensorType.Throughput => "Throughput",
        _ => t.ToString(),
    };

    private static string? FormatText(double? value, SensorType t)
    {
        if (value is not double v) return null;
        var unit = t switch
        {
            SensorType.Voltage => "V",
            SensorType.Current => "A",
            SensorType.Power => "W",
            SensorType.Clock => "MHz",
            SensorType.Temperature => "°C",
            SensorType.Load => "%",
            SensorType.Frequency => "Hz",
            SensorType.Fan => "RPM",
            SensorType.Flow => "L/h",
            SensorType.Control => "%",
            SensorType.Level => "%",
            SensorType.Data => "GB",
            SensorType.SmallData => "MB",
            SensorType.Throughput => "B/s",
            _ => "",
        };
        var num = v.ToString("0.0", System.Globalization.CultureInfo.InvariantCulture);
        return unit.Length == 0 ? num : $"{num} {unit}";
    }

    public void Dispose()
    {
        try { if (_opened) _computer.Close(); } catch { /* ignore */ }
    }

    /// <summary>Walks the hardware tree and calls Update() on each node so sensor
    /// values are refreshed before we read them.</summary>
    private sealed class UpdateVisitor : IVisitor
    {
        public void VisitComputer(IComputer computer) => computer.Traverse(this);
        public void VisitHardware(IHardware hardware)
        {
            hardware.Update();
            foreach (var sub in hardware.SubHardware) sub.Accept(this);
        }
        public void VisitSensor(ISensor sensor) { }
        public void VisitParameter(IParameter parameter) { }
    }
}
