namespace TempMonitorAgent.Telemetry;

/// <summary>A single hardware read: the best CPU temperature plus every flattened
/// leaf sensor. Behind an interface so tests can inject a fake source with no driver.</summary>
public readonly record struct SensorSnapshot(double? CpuTemp, List<SensorReading> Sensors);

public interface ISensorSource : IDisposable
{
    /// <summary>Refresh hardware and return the current snapshot. Never throws;
    /// returns an empty snapshot (null temp, empty list) on failure.</summary>
    SensorSnapshot Read();
}
