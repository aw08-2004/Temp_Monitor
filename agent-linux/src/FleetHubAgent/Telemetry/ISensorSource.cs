namespace FleetHubAgent.Telemetry;

/// <summary>A single hardware read: the best CPU temperature plus every flattened leaf
/// sensor.</summary>
public readonly record struct SensorSnapshot(double? CpuTemp, List<SensorReading> Sensors);

/// <summary>Behind an interface for the same reason the Windows agent's is: so the reporting
/// path can be tested against a fake source, with no /sys tree and no hardware.</summary>
public interface ISensorSource : IDisposable
{
    /// <summary>Refresh and return the current snapshot. **Never throws** -- returns an empty
    /// snapshot (null temp, empty list) on failure. Every caller is an unattended loop, and a
    /// sensor read is the single most hardware-dependent thing this agent does; letting it
    /// throw would let one unusual motherboard stop a machine reporting at all.</summary>
    SensorSnapshot Read();
}
