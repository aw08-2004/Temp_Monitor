using System.Diagnostics;
using System.Runtime.Versioning;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging;
using TempMonitorAgent.State;

namespace TempMonitorAgent.DiskUsage;

/// <summary>
/// Scans every fixed NTFS volume once a day and leaves a report for the hub (roadmap #27).
///
/// <para><b>Daily, and the clock survives a restart.</b> Every other reporter keeps its
/// last-run time in memory, so a service restart rescans at once. That is free for a
/// registry walk. For a full read of the MFT it would mean every agent update, and every
/// reboot of a PC that reboots daily, costs a scan. So the time of the last scan is written
/// to <see cref="AgentConfig.DiskUsageDir"/>. A machine that has never scanned waits a random
/// 5-60 minutes first, so a fleet that takes this release in the same quarter hour does not
/// read every disk in the building at once.</para>
///
/// <para><b>Off the inventory loop's thread.</b> A scan takes seconds on an SSD and can take
/// minutes on a busy spinning disk. The inventory loop only starts it; the scan runs on its
/// own below-normal thread and leaves a payload behind. Its neighbours on that loop (the
/// BitLocker escrow, the patch scan) are never queued behind it.</para>
///
/// <para><b>Two uploads, neither on the heartbeat.</b> The summary (volume totals, the folder
/// change list, new large files) is a small JSON POST. The full-depth history is a separate
/// gzip upload per volume (<see cref="TreeDelta"/>): every path whose size changed since the
/// last scan, or every path when the hub asks for a full tree. The heartbeat is the call that
/// decides whether this machine reads online, and neither belongs in it. Failed uploads are
/// retried every five minutes.</para>
///
/// <para><b>The hub can only apply a delta to the scan it was taken against.</b> Each upload
/// names its base: the scan time of the tree it was diffed from. If the hub never received
/// that one (a lost upload, a day the PC was off before the upload finished, a reinstalled
/// hub), it answers 409, and the agent rebuilds the upload as a full tree and sends that
/// instead. Without the base check a lost day would leave the hub's history quietly wrong
/// for every path that changed that day, forever.</para>
///
/// <para><b>Two trees are kept per volume</b>: <c>&lt;letter&gt;.cur</c> and
/// <c>&lt;letter&gt;.prev</c>. The browser's sizes come from the first. The diffs are worked
/// out here because only this machine holds both full trees.</para>
/// </summary>
[SupportedOSPlatform("windows")]
internal static class DiskUsageReporter
{
    internal static readonly TimeSpan ScanInterval = TimeSpan.FromHours(24);

    /// <summary>A scan that failed outright is retried after this long rather than tomorrow.</summary>
    private static readonly TimeSpan FailedRetry = TimeSpan.FromHours(6);

    private static readonly TimeSpan UploadRetry = TimeSpan.FromMinutes(5);

    private static readonly Lock Gate = new();
    private static DateTimeOffset? _nextScan;
    private static bool _running;
    private static JsonObject? _pending;
    private static DateTimeOffset _nextUpload = DateTimeOffset.MinValue;
    private static DateTimeOffset _nextHistoryUpload = DateTimeOffset.MinValue;

    private static string StatePath => Path.Combine(AgentConfig.DiskUsageDir, "state.json");

    /// <summary>Start a scan if one is due and none is running. Returns at once.</summary>
    public static void RefreshIfDue(ILogger log)
    {
        FolderTreeCache.Trim();
        lock (Gate)
        {
            if (_running) return;
            _nextScan ??= LoadNextScan();
            if (DateTimeOffset.UtcNow < _nextScan) return;
            _running = true;
            // MaxValue marks "set by this scan's end". Invalidate() overwrites it with
            // MinValue, which RunScan then leaves alone so the button press is not lost.
            _nextScan = DateTimeOffset.MaxValue;
        }

        var thread = new Thread(() => RunScan(log))
        {
            IsBackground = true,
            Priority = ThreadPriority.BelowNormal,
            Name = "disk-usage-scan",
        };
        thread.Start();
    }

    /// <summary>Scan at the next inventory pass, whatever the clock says. The console's
    /// "Scan now" button.</summary>
    public static void Invalidate()
    {
        lock (Gate) { _nextScan = DateTimeOffset.MinValue; }
    }

    /// <summary>The report waiting for upload, or null. Only offered once the retry back-off
    /// has passed. Stays pending until <see cref="AckSent"/>.</summary>
    public static JsonObject? TakePending()
    {
        lock (Gate)
        {
            if (_pending is null || DateTimeOffset.UtcNow < _nextUpload) return null;
            _nextUpload = DateTimeOffset.UtcNow + UploadRetry;
            return _pending;
        }
    }

    /// <summary>Clear <paramref name="sent"/> if it is still the pending report. A scan that
    /// finished while the upload was in flight is newer and stays pending, the same rule
    /// SoftwareInventoryReporter.AckSent explains.</summary>
    public static void AckSent(JsonObject sent)
    {
        lock (Gate)
        {
            if (ReferenceEquals(_pending, sent)) _pending = null;
        }
    }

    private static void RunScan(ILogger log)
    {
        var started = DateTimeOffset.UtcNow;
        var next = started + ScanInterval;
        try
        {
            var note = StateDirectory.HardenPrivate(AgentConfig.DiskUsageDir);
            if (note is not null) log.LogWarning("{Note}", note);

            var volumes = new JsonArray();
            var skipped = new JsonArray();
            foreach (var drive in DriveInfo.GetDrives())
            {
                if (drive.DriveType != DriveType.Fixed) continue;
                var letter = drive.Name.TrimEnd('\\').ToUpperInvariant();
                var reason = SkipReason(drive);
                if (reason is not null)
                {
                    skipped.Add(new JsonObject { ["volume"] = letter, ["reason"] = reason });
                    continue;
                }
                try
                {
                    volumes.Add(ScanVolume(drive, letter, log));
                }
                catch (Exception e)
                {
                    log.LogWarning("Disk usage scan of {Volume} failed: {Msg}", letter, e.Message);
                    skipped.Add(new JsonObject { ["volume"] = letter, ["reason"] = "error", ["error"] = e.Message });
                }
            }

            var payload = new JsonObject
            {
                ["scanned_at"] = started.ToUnixTimeSeconds(),
                ["volumes"] = volumes,
                ["skipped"] = skipped,
            };
            lock (Gate)
            {
                _pending = payload;
                _nextUpload = DateTimeOffset.MinValue;
                _nextHistoryUpload = DateTimeOffset.MinValue;
            }
            if (volumes.Count == 0 && skipped.Count > 0) next = started + FailedRetry;
        }
        catch (Exception e)
        {
            log.LogWarning(e, "Disk usage scan failed");
            next = started + FailedRetry;
        }
        finally
        {
            SaveNextScan(next, log);
            lock (Gate)
            {
                // "Scan now" pressed during this scan still wins over the new clock.
                if (_nextScan == DateTimeOffset.MaxValue) _nextScan = next;
                _running = false;
            }
        }
    }

    /// <summary>Why a fixed drive is not scanned, or null if it is. The reason goes to the
    /// hub, so the console can say "D: is ReFS" instead of quietly showing no data.</summary>
    internal static string? SkipReason(DriveInfo drive)
    {
        try
        {
            // Not ready covers a BitLocker volume that is still locked.
            if (!drive.IsReady) return "not_ready";
            if (!string.Equals(drive.DriveFormat, "NTFS", StringComparison.OrdinalIgnoreCase))
                return "filesystem:" + drive.DriveFormat;
            return null;
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            return "not_ready";
        }
    }

    private static JsonObject ScanVolume(DriveInfo drive, string letter, ILogger log)
    {
        var dir = AgentConfig.DiskUsageDir;
        var stem = letter.TrimEnd(':');
        var curPath = Path.Combine(dir, stem + ".cur");
        var prevPath = Path.Combine(dir, stem + ".prev");

        var watch = Stopwatch.StartNew();
        var tree = MftVolumeReader.Scan(letter, drive.TotalSize, drive.TotalFreeSpace, CancellationToken.None);
        watch.Stop();

        var before = FolderTree.Load(curPath);
        var payload = VolumePayload(before, tree, watch.ElapsedMilliseconds);

        // The history upload. Diffed against the tree the hub should already hold; when there
        // is none, this is a full tree. A delta still waiting from an earlier scan is
        // replaced: it was taken against a base the hub has not got either, so the hub will
        // answer 409 and the next attempt becomes a full tree anyway.
        var lines = TreeDelta.Write(before, tree, UploadPath(letter));
        WriteUploadMeta(letter, tree.ScannedAt, before?.ScannedAt, full: before is null);

        // Rotate only after the new tree is safely written, so a crash in between leaves
        // yesterday's .cur in place instead of nothing at all.
        tree.Save(curPath + ".new");
        if (File.Exists(curPath)) File.Move(curPath, prevPath, overwrite: true);
        File.Move(curPath + ".new", curPath, overwrite: true);
        FolderTreeCache.Forget(letter);

        using var self = Process.GetCurrentProcess();
        log.LogInformation(
            "Disk usage: scanned {Volume} in {Ms} ms, {Folders} folders, {Files} files, {Lines} history line(s), peak working set {Mb} MB",
            letter, watch.ElapsedMilliseconds, tree.Count, tree.FileCount, lines,
            self.PeakWorkingSet64 / (1024 * 1024));
        return payload;
    }

    /// <summary>
    /// The wire shape for one volume. Pure, so a test can pin exactly what hub/disk_usage.py
    /// will parse; that file asserts the same field names from the other side.
    /// </summary>
    internal static JsonObject VolumePayload(FolderTree? before, FolderTree after, long durationMs)
    {
        var changes = new JsonArray();
        foreach (var c in FolderTreeDiff.Compare(before, after))
            changes.Add(new JsonObject { ["path"] = c.Path, ["before"] = c.Before, ["after"] = c.After });

        var large = new JsonArray();
        foreach (var f in FolderTreeDiff.NewLargeFiles(before, after))
            large.Add(new JsonObject { ["path"] = f.Path, ["size"] = f.Size, ["allocated"] = f.Allocated });

        return new JsonObject
        {
            ["volume"] = after.Volume,
            ["fs"] = "NTFS",
            ["scanned_at"] = after.ScannedAt.ToUnixTimeSeconds(),
            ["previous_scanned_at"] = before?.ScannedAt.ToUnixTimeSeconds(),
            ["duration_ms"] = durationMs,
            ["total_bytes"] = after.TotalBytes,
            ["free_bytes"] = after.FreeBytes,
            ["used_bytes"] = Math.Max(0, after.TotalBytes - after.FreeBytes),
            ["folders"] = after.Count,
            ["files"] = after.Files[0],
            ["changes"] = changes,
            ["new_large_files"] = large,
        };
    }

    // ------------------------------------------------------------------ history upload

    private static string UploadPath(string letter) =>
        Path.Combine(AgentConfig.DiskUsageDir, letter.TrimEnd(':') + ".upload.gz");

    private static string UploadMetaPath(string letter) =>
        Path.Combine(AgentConfig.DiskUsageDir, letter.TrimEnd(':') + ".upload.json");

    private static void WriteUploadMeta(string letter, DateTimeOffset scannedAt,
                                        DateTimeOffset? baseScannedAt, bool full)
    {
        File.WriteAllText(UploadMetaPath(letter), new JsonObject
        {
            ["volume"] = letter,
            ["scanned_at"] = scannedAt.ToUnixTimeSeconds(),
            ["base"] = baseScannedAt?.ToUnixTimeSeconds(),
            ["full"] = full,
        }.ToJsonString());
    }

    /// <summary>
    /// Send every waiting history upload. Called from the inventory loop after the summary.
    ///
    /// Throttled by its own clock, not the summary's: a full tree of a big drive can take
    /// minutes on a slow line, and retrying it every fifteen seconds after a failure would
    /// make that line slower for the person at the PC.
    /// </summary>
    public static async Task UploadHistoryAsync(Fleet.FleetClient fleet, ILogger log,
                                                CancellationToken ct)
    {
        lock (Gate)
        {
            if (_running || DateTimeOffset.UtcNow < _nextHistoryUpload) return;
            _nextHistoryUpload = DateTimeOffset.UtcNow + UploadRetry;
        }
        string[] metas;
        try { metas = Directory.GetFiles(AgentConfig.DiskUsageDir, "*.upload.json"); }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException) { return; }

        foreach (var metaPath in metas)
        {
            JsonNode? meta;
            try { meta = JsonNode.Parse(File.ReadAllText(metaPath)); }
            catch (Exception e) when (e is IOException or JsonException) { continue; }
            var letter = meta?["volume"]?.GetValue<string>();
            if (string.IsNullOrEmpty(letter) || !File.Exists(UploadPath(letter))) continue;

            var outcome = await fleet.UploadDiskHistoryAsync(UploadPath(letter), letter,
                meta!["scanned_at"]!.GetValue<long>(), meta["base"]?.GetValue<long>(),
                meta["full"]?.GetValue<bool>() == true, ct);
            if (outcome == Fleet.FleetClient.HistoryUpload.Stored)
            {
                TryDelete(UploadPath(letter));
                TryDelete(metaPath);
            }
            else if (outcome == Fleet.FleetClient.HistoryUpload.NeedFull)
            {
                // The hub does not hold the base this delta was taken against. Resend the
                // whole tree, which needs no base. Built from .cur, the tree this upload
                // already describes, so nothing is rescanned.
                var cur = FolderTree.Load(Path.Combine(AgentConfig.DiskUsageDir,
                                                       letter.TrimEnd(':') + ".cur"));
                if (cur is null) { TryDelete(UploadPath(letter)); TryDelete(metaPath); continue; }
                log.LogInformation("Disk usage: hub asked for the full tree of {Volume}", letter);
                TreeDelta.Write(null, cur, UploadPath(letter));
                WriteUploadMeta(letter, cur.ScannedAt, null, full: true);
                lock (Gate) { _nextHistoryUpload = DateTimeOffset.MinValue; }
            }
        }
    }

    private static void TryDelete(string path)
    {
        try { File.Delete(path); }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException) { }
    }

    private static DateTimeOffset LoadNextScan()
    {
        try
        {
            if (File.Exists(StatePath))
            {
                var node = JsonNode.Parse(File.ReadAllText(StatePath));
                var next = node?["next_scan"]?.GetValue<long>();
                if (next is not null) return DateTimeOffset.FromUnixTimeSeconds(next.Value);
            }
        }
        catch (Exception e) when (e is IOException or JsonException or InvalidOperationException
                                    or UnauthorizedAccessException or FormatException) { }
        return DateTimeOffset.UtcNow + TimeSpan.FromMinutes(Random.Shared.Next(5, 61));
    }

    private static void SaveNextScan(DateTimeOffset next, ILogger log)
    {
        try
        {
            Directory.CreateDirectory(AgentConfig.DiskUsageDir);
            File.WriteAllText(StatePath,
                new JsonObject { ["next_scan"] = next.ToUnixTimeSeconds() }.ToJsonString());
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            log.LogDebug("Could not persist the disk usage clock: {Msg}", e.Message);
        }
    }
}
