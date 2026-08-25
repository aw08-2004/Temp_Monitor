using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging;
using TempMonitorAgent.Fleet;
using TempMonitorAgent.Fleet.Executors;

namespace TempMonitorAgent.Files;

/// <summary>
/// list_directory: what is in one folder on this machine, or what volumes it has.
///
/// **The answer does not come back as the command's output.** It is POSTed to the hub's
/// listing endpoint against the request id that came with the command, and the command
/// completes with a one-line summary. A folder of two thousand entries is around 200 KB and
/// the command channel's output is a terminal transcript truncated at 16,000 characters —
/// but the reason is not only size. Keeping them separate means "the machine answered" and
/// "the answer is stored" are two facts the console can tell apart, so a listing that was
/// enumerated but lost to a dropped connection does not look like a folder that is empty.
///
/// **Enumeration never stops on one bad entry.** A directory an operator is browsing will
/// contain junctions they cannot follow, files whose ACLs deny a stat, and names left by
/// software that had no business writing them. Windows Explorer shows the rest; so does
/// this. An entry we cannot describe is skipped, and the folder still lists.
/// </summary>
public sealed class ListDirectoryExecutor : ICommandExecutor
{
    /// <summary>Matches hub/files.py MAX_ENTRIES. The hub caps again on arrival — this cap
    /// is the one that stops the machine building and sending the megabyte in the first
    /// place, which is the cost that actually matters on a DSL line.</summary>
    private const int MaxEntries = 2000;

    private readonly ILogger<ListDirectoryExecutor> _log;
    private readonly FleetClient _fleet;

    public ListDirectoryExecutor(ILogger<ListDirectoryExecutor> log, FleetClient fleet)
    {
        _log = log;
        _fleet = fleet;
    }

    public string Type => "list_directory";

    public async Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput,
                                                  CancellationToken ct)
    {
        var requestId = cmd.Params.GetString("request_id") ?? "";
        if (requestId.Length == 0)
            return CommandResult.Fail("No listing request id was supplied.");

        if (cmd.Params.GetBool("drives"))
        {
            var drives = ReadDrives();
            var payload = new JsonObject { ["entries"] = new JsonArray(), ["drives"] = drives };
            await _fleet.ReportListingAsync(requestId, payload, ct);
            return CommandResult.Ok($"{drives.Count} drive(s)");
        }

        var path = cmd.Params.GetString("path") ?? "";
        var refusal = PathRules.Reject(path);
        if (refusal is not null)
        {
            await ReportFailureAsync(requestId, refusal, ct);
            return CommandResult.Fail(refusal);
        }
        path = PathRules.Normalize(path);

        try
        {
            var (entries, truncated) = ReadFolder(path, ct);
            var payload = new JsonObject
            {
                ["path"] = path,
                ["entries"] = entries,
                ["truncated"] = truncated,
            };
            if (!await _fleet.ReportListingAsync(requestId, payload, ct))
                return CommandResult.Fail("The hub would not accept the listing.");
            var note = truncated > 0
                ? $"{entries.Count} item(s), {truncated} not sent"
                : $"{entries.Count} item(s)";
            return CommandResult.Ok(note);
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException
                                    or System.Security.SecurityException
                                    or ArgumentException or NotSupportedException)
        {
            // A refusal is a RESULT, not a fault: "Access is denied" is the answer to "what
            // is in this folder", and the console renders it as one. Reported to the listing
            // row as well as returned, so an operator watching the file pane sees it there
            // rather than only in a command history they are not looking at.
            await ReportFailureAsync(requestId, e.Message, ct);
            return CommandResult.Fail(e.Message);
        }
    }

    private async Task ReportFailureAsync(string requestId, string error, CancellationToken ct)
    {
        try
        {
            await _fleet.ReportListingAsync(requestId, new JsonObject { ["error"] = error }, ct);
        }
        catch (Exception e)
        {
            // Best effort by design. The command result still carries the reason, and
            // failing the command over a failed report would tell the operator the listing
            // broke twice when it broke once.
            _log.LogDebug("Could not report a listing failure: {Msg}", e.Message);
        }
    }

    /// <summary>One folder's contents, folders first then names, capped. Returns the
    /// entries and how many were dropped.</summary>
    private static (JsonArray Entries, int Truncated) ReadFolder(string path, CancellationToken ct)
    {
        var dir = new DirectoryInfo(path);
        // EnumerateFileSystemInfos rather than GetFileSystemInfos: the latter materialises
        // the whole of System32 before the cap can apply to any of it.
        var all = new List<FileSystemInfo>();
        foreach (var info in dir.EnumerateFileSystemInfos())
        {
            ct.ThrowIfCancellationRequested();
            all.Add(info);
        }

        // Sorted BEFORE truncating, so what survives the cap is the top of the list the
        // operator is reading rather than whatever the filesystem handed us first.
        all.Sort((a, b) =>
        {
            var aDir = a.Attributes.HasFlag(FileAttributes.Directory);
            var bDir = b.Attributes.HasFlag(FileAttributes.Directory);
            if (aDir != bDir) return aDir ? -1 : 1;
            return string.Compare(a.Name, b.Name, StringComparison.OrdinalIgnoreCase);
        });

        var entries = new JsonArray();
        foreach (var info in all)
        {
            if (entries.Count >= MaxEntries) break;
            var entry = Describe(info);
            if (entry is not null) entries.Add(entry);
        }
        return (entries, Math.Max(0, all.Count - entries.Count));
    }

    /// <summary>One entry, or null if it cannot be described.
    ///
    /// Every property read here can throw on a file that vanished between the enumeration
    /// and now — a browser's cache directory does that continuously — so the whole thing is
    /// guarded and a lost race drops one row instead of the folder.</summary>
    private static JsonObject? Describe(FileSystemInfo info)
    {
        try
        {
            var attrs = info.Attributes;
            var isDir = attrs.HasFlag(FileAttributes.Directory);
            var entry = new JsonObject
            {
                ["name"] = info.Name,
                ["directory"] = isDir,
                ["hidden"] = attrs.HasFlag(FileAttributes.Hidden),
                ["system"] = attrs.HasFlag(FileAttributes.System),
                ["readonly"] = attrs.HasFlag(FileAttributes.ReadOnly),
                // A junction or symlink is flagged because acting on one does not do what
                // it looks like: copying it copies the target, deleting it does not.
                ["link"] = attrs.HasFlag(FileAttributes.ReparsePoint),
                ["modified"] = new DateTimeOffset(info.LastWriteTimeUtc).ToUnixTimeSeconds(),
            };
            // Deliberately absent for a folder rather than 0: the size of a directory entry
            // is not the size of what is in it, and rendering "0 bytes" beside a folder
            // holding a gigabyte is a lie the console would have no way to detect.
            if (!isDir && info is FileInfo file) entry["size"] = file.Length;
            return entry;
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            return null;
        }
    }

    /// <summary>This machine's volumes, for the explorer's root view.
    ///
    /// Unready drives (an empty optical bay, a disconnected mapped share) are listed WITHOUT
    /// their sizes rather than omitted. An operator who expected to see D: needs to see that
    /// it is there and not ready — a drive that silently vanishes from the list looks like a
    /// console that has not finished loading.</summary>
    private static JsonArray ReadDrives()
    {
        var drives = new JsonArray();
        DriveInfo[] all;
        try { all = DriveInfo.GetDrives(); }
        catch (IOException) { return drives; }

        foreach (var drive in all)
        {
            try
            {
                var entry = new JsonObject
                {
                    ["path"] = PathRules.Normalize(drive.Name),
                    ["type"] = drive.DriveType.ToString().ToLowerInvariant(),
                };
                if (drive.IsReady)
                {
                    entry["label"] = drive.VolumeLabel;
                    entry["total_bytes"] = drive.TotalSize;
                    entry["free_bytes"] = drive.AvailableFreeSpace;
                }
                drives.Add(entry);
            }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException)
            {
                // One unreachable volume must not cost the operator the other five.
            }
        }
        return drives;
    }
}
