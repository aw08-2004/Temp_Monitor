using LibreHardwareMonitor.Hardware;
using Microsoft.Extensions.Logging;

namespace TempMonitorAgent.Telemetry;

/// <summary>
/// Reads sensors in-process via LibreHardwareMonitorLib (no separate
/// LibreHardwareMonitor.exe / :8085 web server). Requires the PawnIO kernel driver
/// (installed by the installer) and runs fine under a SYSTEM service. Faithfully
/// mirrors companion.py's flatten_sensors + _walk/pick_cpu_temp selection.
/// </summary>
public sealed class SensorReader : ISensorSource
{
    // Best-first CPU temperature preference, matched as a substring of the sensor
    // name (lowercased) — identical to companion.py PREFERRED_SENSORS.
    private static readonly string[] PreferredSensors =
    {
        "cpu package",
        "core (tctl/tdie)",
        "core average",
        "core max",
        "cpu cores",
    };

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

        try
        {
            EnsureOpen();
            _computer.Accept(_visitor); // updates every hardware + sub-hardware

            foreach (var hw in _computer.Hardware)
                CollectHardware(hw, sensors, cpuTemps);
        }
        catch (Exception e)
        {
            _log.LogWarning(e, "Sensor read failed");
            return new SensorSnapshot(null, sensors);
        }

        return new SensorSnapshot(PickCpuTemp(cpuTemps), sensors);
    }

    private void CollectHardware(IHardware hw, List<SensorReading> sensors,
                                 List<(string, double)> cpuTemps)
    {
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
            CollectHardware(sub, sensors, cpuTemps);
    }

    private static double? PickCpuTemp(List<(string name, double value)> cpuTemps)
    {
        if (cpuTemps.Count == 0) return null;
        foreach (var wanted in PreferredSensors)
            foreach (var (name, value) in cpuTemps)
                if (name.Contains(wanted))
                    return value;
        return cpuTemps[0].value; // any CPU temp beats none
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
