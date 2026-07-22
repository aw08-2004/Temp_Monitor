using System.Management;
using Microsoft.Extensions.Logging;

namespace TempMonitorAgent.Backup;

/// <summary>
/// Volume Shadow Copy snapshots, so files that are open are still captured. Roadmap #1b.
///
/// Without this, the files most worth backing up are the ones most likely to fail: an
/// Outlook PST is held open all day, and so is whatever document someone left up over
/// lunch. Reading them live gives either a sharing violation or — worse — a torn read that
/// looks like a successful backup until you try to open it.
///
/// **`vssadmin create shadow` is deliberately not used: it is Server-only.** On Windows
/// client SKUs the supported route is WMI's Win32_ShadowCopy.Create with the
/// "ClientAccessible" context, which is what this does. Files are then read through
/// `\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopyN\...`.
///
/// **Failure is skip-and-report, never fail-the-run.** If a snapshot cannot be created —
/// VSS service disabled, no space for the diff area, a provider error — the caller falls
/// back to reading the live filesystem and the reason is reported alongside the backup.
/// A machine that backs up 99% of its files and says so is worth far more than one that
/// backs up none and says why.
///
/// Snapshots are per volume and are DELETED on dispose. A leaked shadow copy consumes the
/// volume's shadow storage until Windows evicts it, which silently destroys older restore
/// points the user may be relying on — so disposal runs in a finally, always.
/// </summary>
public sealed class VssSnapshot : IDisposable
{
    private readonly ILogger _log;
    private readonly Dictionary<string, string> _deviceByVolume = new(StringComparer.OrdinalIgnoreCase);
    private readonly List<string> _shadowIds = [];
    private readonly List<string> _problems = [];
    private bool _disposed;

    public VssSnapshot(ILogger log) => _log = log;

    /// <summary>Volumes that were successfully snapshotted.</summary>
    public IReadOnlyCollection<string> SnapshottedVolumes => _deviceByVolume.Keys;

    /// <summary>Why a volume is being read live instead. Reported with the run.</summary>
    public IReadOnlyList<string> Problems => _problems;

    /// <summary>
    /// Snapshot the volume holding <paramref name="path"/>, if it is not already done.
    /// Returns true if reads for that volume will now come from a snapshot.
    /// </summary>
    public bool EnsureVolume(string path)
    {
        var volume = VolumeOf(path);
        if (volume is null) return false;
        if (_deviceByVolume.ContainsKey(volume)) return true;

        try
        {
            var device = Create(volume);
            if (device is null)
            {
                _problems.Add($"{volume} could not be snapshotted; reading it live, so any " +
                              "file that is open may be skipped.");
                return false;
            }
            _deviceByVolume[volume] = device;
            _log.LogInformation("VSS snapshot of {Volume} at {Device}", volume, device);
            return true;
        }
        catch (Exception e)
        {
            _problems.Add($"{volume} could not be snapshotted ({e.Message}); reading it " +
                          "live, so any file that is open may be skipped.");
            return false;
        }
    }

    /// <summary>
    /// The path to read <paramref name="path"/> from — inside the snapshot when there is
    /// one, otherwise unchanged so the caller reads live.
    /// </summary>
    public string MapPath(string path)
    {
        var volume = VolumeOf(path);
        if (volume is null || !_deviceByVolume.TryGetValue(volume, out var device)) return path;
        var relative = path[volume.Length..].TrimStart('\\');
        return device + "\\" + relative;
    }

    /// <summary>"C:\" for a local path, or null for UNC — a network share is the file
    /// server's business to snapshot, not ours.</summary>
    private static string? VolumeOf(string path)
    {
        if (string.IsNullOrWhiteSpace(path)) return null;
        if (path.StartsWith(@"\\", StringComparison.Ordinal)) return null;
        var root = Path.GetPathRoot(path);
        return string.IsNullOrEmpty(root) ? null : root;
    }

    private string? Create(string volume)
    {
        using var shadowClass = new ManagementClass("Win32_ShadowCopy");
        var parameters = shadowClass.GetMethodParameters("Create");
        parameters["Volume"] = volume;
        // ClientAccessible is the context available on client SKUs; the alternatives
        // (Backup, AppRollback...) require a full VSS requestor with writer coordination.
        parameters["Context"] = "ClientAccessible";

        var result = shadowClass.InvokeMethod("Create", parameters, null);
        var returnValue = Convert.ToUInt32(result["ReturnValue"]);
        if (returnValue != 0)
        {
            _log.LogWarning("Win32_ShadowCopy.Create({Volume}) returned {Code}", volume, returnValue);
            return null;
        }

        var shadowId = result["ShadowID"]?.ToString();
        if (string.IsNullOrEmpty(shadowId)) return null;
        _shadowIds.Add(shadowId);

        using var searcher = new ManagementObjectSearcher(
            $"SELECT DeviceObject FROM Win32_ShadowCopy WHERE ID = '{shadowId}'");
        foreach (var item in searcher.Get())
        {
            using var shadow = (ManagementObject)item;
            var device = shadow["DeviceObject"]?.ToString();
            if (!string.IsNullOrEmpty(device))
            {
                // DeviceObject comes back as \\?\GLOBALROOT\Device\HarddiskVolumeShadowCopyN
                // with no trailing separator. .NET passes \\?\ paths through without
                // normalisation, so this is usable directly with File/Directory APIs.
                return device.TrimEnd('\\');
            }
        }
        return null;
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        foreach (var id in _shadowIds)
        {
            try
            {
                using var shadow = new ManagementObject(
                    $"Win32_ShadowCopy.ID='{id}'");
                shadow.Delete();
            }
            catch (Exception e)
            {
                // Logged, never thrown: a failure to clean up must not turn a completed
                // backup into a failed one. Windows will evict it eventually.
                _log.LogWarning("Could not delete shadow copy {Id}: {Msg}", id, e.Message);
            }
        }
        _shadowIds.Clear();
        _deviceByVolume.Clear();
    }
}
