namespace FleetHubAgent.Telemetry;

/// <summary>A single hardware read: the best temperature to report as this machine's "temp",
/// plus every flattened leaf sensor.
///
/// The property is called <c>CpuTemp</c> to match the other two agents, and on Android it is
/// frequently NOT a CPU temperature -- see AndroidSensorReader, which explains what it falls
/// back to and why that is the honest answer for a phone.</summary>
public readonly record struct SensorSnapshot(double? CpuTemp, List<SensorReading> Sensors);

/// <summary>Behind an interface for the same reason the other agents' is: so the reporting
/// path can be tested against a fake source, with no device and no hardware.</summary>
public interface ISensorSource : IDisposable
{
    /// <summary>Refresh and return the current snapshot. **Never throws** -- returns an empty
    /// snapshot (null temp, empty list) on failure. Every caller is an unattended loop, and a
    /// sensor read is the single most hardware-dependent thing this agent does; letting it
    /// throw would let one unusual device stop reporting altogether.</summary>
    SensorSnapshot Read();
}

/// <summary>How long the device has been up.
///
/// **Separate from ISensorSource, and separate from anything in the Linux agent, because the
/// right answer on Android is a different question.** The Linux agent reads /proc/uptime and
/// explains at length why it does not use a monotonic tick count. Android's
/// SystemClock.ElapsedRealtime() is exactly the number wanted -- milliseconds since boot,
/// INCLUDING the time the device spent in deep sleep, which for a phone is most of them. A
/// phone that is up for a week and awake for four hours has been up for a week, and that is
/// what an operator looking at an uptime column is asking.</summary>
public interface IUptimeSource
{
    /// <summary>Seconds since boot, or null when it cannot be determined. Never throws.</summary>
    long? UptimeSeconds();
}
