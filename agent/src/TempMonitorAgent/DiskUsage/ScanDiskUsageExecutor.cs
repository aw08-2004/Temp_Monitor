using System.Runtime.Versioning;
using TempMonitorAgent.Fleet;
using TempMonitorAgent.Fleet.Executors;

namespace TempMonitorAgent.DiskUsage;

/// <summary>
/// scan_disk_usage: the console's "Scan now" (roadmap #27).
///
/// **Completes at once, and says only that the scan is queued.** A scan can take minutes,
/// and holding a command open that long would keep a dispatcher slot and an operator's
/// spinner busy for a result that arrives by its own POST anyway. The console watches the
/// scan time change instead, so this command saying "queued" is the honest answer.
/// </summary>
[SupportedOSPlatform("windows")]
public sealed class ScanDiskUsageExecutor : ICommandExecutor
{
    public string Type => "scan_disk_usage";

    public Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput,
                                            CancellationToken ct)
    {
        DiskUsageReporter.Invalidate();
        return Task.FromResult(CommandResult.Ok("Disk usage scan queued"));
    }
}
