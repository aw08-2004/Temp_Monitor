using System.IO.Compression;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging;
using TempMonitorAgent.Fleet;
using TempMonitorAgent.Fleet.Executors;

namespace TempMonitorAgent.Files;

/// <summary>
/// Where bytes in flight are staged, and why they are staged at all.
///
/// Both directions go through a file under ProgramData rather than streaming straight
/// between the disk and the socket, for two different reasons that both matter:
///
///   * **Upward**, because the file being fetched is a file somebody is USING. Opening
///     a locked .pst or an open Word document with an exclusive share fails outright, and
///     holding one open for the length of a slow upload keeps it locked for exactly as long.
///     A copy taken with FileShare.ReadWrite is read in seconds and released, and the upload
///     then contends with nothing.
///   * **Downward**, because a half-arrived download must not be indistinguishable from a
///     finished one. The bytes land in staging and are moved into place only when they are
///     all there, so an interrupted push leaves the destination folder exactly as it was
///     rather than holding a truncated installer somebody will later run.
///
/// The directory is cleaned opportunistically rather than on a timer: each run removes its
/// own staging file in a finally, and a leftover from a process that was killed mid-transfer
/// is swept by the next run of the same executor. A machine that is never asked to transfer
/// anything again keeps one file, which is the correct trade against a background thread.
/// </summary>
internal static class TransferStaging
{
    internal static string Dir => Path.Combine(AgentConfig.ProgramDataDir, "files");

    internal static string Reserve(string transferId)
    {
        Directory.CreateDirectory(Dir);
        Sweep();
        return Path.Combine(Dir, $"{transferId}.part");
    }

    internal static void Discard(string path)
    {
        try { if (File.Exists(path)) File.Delete(path); }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException) { }
    }

    /// <summary>Remove staging files older than a day — the debris of a transfer whose
    /// process died mid-flight. An hour would be the hub's expiry, but a slow upload over a
    /// bad link can legitimately still be running, and deleting the file underneath it would
    /// turn a slow transfer into a failed one.</summary>
    private static void Sweep()
    {
        try
        {
            var cutoff = DateTime.UtcNow.AddDays(-1);
            foreach (var file in new DirectoryInfo(Dir).EnumerateFiles("*.part"))
            {
                if (file.LastWriteTimeUtc < cutoff) Discard(file.FullName);
            }
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException) { }
    }
}

/// <summary>
/// fetch_file: send one file — or one folder, zipped — up to the hub so an operator can
/// download it.
///
/// **A folder becomes a zip on THIS machine, not on the hub.** There is no other place it
/// could happen: the hub never sees the folder, only the bytes, and zipping here is also
/// what makes a 200 MB source tree a 30 MB transfer over the link that is actually slow.
/// The hub is told to expect a .zip when the command is issued, so the name the operator's
/// browser saves matches what is inside it.
///
/// **The size cap is enforced before the upload starts, not during it.** Discovering at
/// 1.9 GB that a folder was going to be 4 GB wastes the whole transfer and the operator's
/// wait; measuring first costs a directory walk. What cannot be measured first is the zip's
/// compressed size, so a folder is capped on its UNCOMPRESSED total — which is the
/// conservative direction, and the direction that cannot surprise the hub.
/// </summary>
public sealed class FetchFileExecutor : ICommandExecutor
{
    private readonly ILogger<FetchFileExecutor> _log;
    private readonly FleetClient _fleet;

    public FetchFileExecutor(ILogger<FetchFileExecutor> log, FleetClient fleet)
    {
        _log = log;
        _fleet = fleet;
    }

    public string Type => "fetch_file";

    public async Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput,
                                                  CancellationToken ct)
    {
        var transferId = cmd.Params.GetString("transfer_id") ?? "";
        var url = cmd.Params.GetString("url") ?? "";
        var path = cmd.Params.GetString("path") ?? "";
        var folder = (cmd.Params.GetString("kind") ?? "file") == "folder";
        // The hub's own ceiling, carried in the command rather than hardcoded here, so
        // raising it there does not need an agent release. Zero means "the hub did not say",
        // which only an older hub does, and a cap of two gigabytes is the safe reading.
        long maxBytes = cmd.Params.GetInt("max_bytes", 0);
        if (maxBytes <= 0) maxBytes = 2L * 1024 * 1024 * 1024;

        if (transferId.Length == 0 || url.Length == 0)
            return CommandResult.Fail("This fetch is missing its transfer details.");

        var refusal = PathRules.Reject(path);
        if (refusal is not null)
            return await FailAsync(transferId, refusal, ct);
        path = PathRules.Normalize(path);

        var staged = TransferStaging.Reserve(transferId);
        try
        {
            long size;
            if (folder)
            {
                if (!Directory.Exists(path))
                    return await FailAsync(transferId, $"{path} is not a folder.", ct);
                var total = MeasureTree(path, maxBytes, ct);
                if (total < 0)
                    return await FailAsync(transferId,
                        "That folder holds more than this hub will carry.", ct);
                onOutput?.Invoke($"[files] zipping {path} ({Mb(total)})");
                ZipFile.CreateFromDirectory(path, staged, CompressionLevel.Optimal,
                                            includeBaseDirectory: false);
                size = new FileInfo(staged).Length;
            }
            else
            {
                if (!File.Exists(path))
                    return await FailAsync(transferId, $"{path} is not a file.", ct);
                var length = new FileInfo(path).Length;
                if (length > maxBytes)
                    return await FailAsync(transferId,
                        "That file is larger than this hub will carry.", ct);
                // FileShare.ReadWrite: the whole point of taking a copy is to read a file
                // that something else has open, so this must not be the one request that
                // demands exclusivity.
                await using (var src = new FileStream(path, FileMode.Open, FileAccess.Read,
                                                      FileShare.ReadWrite, 1024 * 1024,
                                                      useAsync: true))
                await using (var dst = new FileStream(staged, FileMode.Create, FileAccess.Write,
                                                      FileShare.None, 1024 * 1024,
                                                      useAsync: true))
                {
                    await src.CopyToAsync(dst, ct);
                }
                size = new FileInfo(staged).Length;
            }

            onOutput?.Invoke($"[files] uploading {Mb(size)}");
            var error = await _fleet.UploadFileAsync(url, staged, viaHub: true, ct);
            if (error is not null)
                return await FailAsync(transferId, error, ct);
            return CommandResult.Ok($"[files] sent {path} ({Mb(size)})");
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException
                                    or NotSupportedException)
        {
            _log.LogWarning(e, "fetch_file failed for {Path}", path);
            return await FailAsync(transferId, e.Message, ct);
        }
        finally
        {
            TransferStaging.Discard(staged);
        }
    }

    /// <summary>Total bytes under a folder, or -1 once it passes <paramref name="cap"/>.
    ///
    /// Stops counting at the cap rather than measuring a 400 GB tree to conclude it is too
    /// big. Reparse points are skipped for the reason they are skipped in a copy: a junction
    /// into C:\ makes the walk unbounded.</summary>
    private static long MeasureTree(string path, long cap, CancellationToken ct)
    {
        long total = 0;
        var stack = new Stack<DirectoryInfo>();
        stack.Push(new DirectoryInfo(path));
        while (stack.Count > 0)
        {
            ct.ThrowIfCancellationRequested();
            var dir = stack.Pop();
            try
            {
                foreach (var file in dir.EnumerateFiles())
                {
                    total += file.Length;
                    if (total > cap) return -1;
                }
                foreach (var sub in dir.EnumerateDirectories())
                {
                    if (!sub.Attributes.HasFlag(FileAttributes.ReparsePoint)) stack.Push(sub);
                }
            }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException)
            {
                // A subfolder we cannot read contributes nothing and stops nothing: the zip
                // below will skip it too, and the operator gets what could be read.
            }
        }
        return total;
    }

    private async Task<CommandResult> FailAsync(string transferId, string reason,
                                                CancellationToken ct)
    {
        // Told to the hub as well as returned, because the console is polling the TRANSFER,
        // not the command. Without this it would wait out the full hour before deciding
        // nothing was coming.
        await _fleet.ReportTransferAsync(transferId, new JsonObject { ["error"] = reason }, ct);
        return CommandResult.Fail($"[files] {reason}");
    }

    private static string Mb(long bytes) => $"{bytes / 1024.0 / 1024.0:F1} MB";
}

/// <summary>
/// push_file: collect one file the operator uploaded and put it on this machine's disk.
///
/// **The destination is created if it is not there.** "Put this in C:\Temp\drivers" on a
/// machine with no such folder is a request to make one, not an error — and the alternative
/// is telling an operator to issue a new_folder first, for a folder they only care about as
/// somewhere to put a file.
///
/// **An existing file is NOT replaced unless the operator said so.** Overwriting is the one
/// thing here that destroys data, and it is the one thing a helpdesk does by accident: the
/// same installer pushed twice, the same config file, the same name. So it takes a flag the
/// console asks for explicitly, and the default refusal names the file that is in the way.
/// </summary>
public sealed class PushFileExecutor : ICommandExecutor
{
    private readonly ILogger<PushFileExecutor> _log;
    private readonly FleetClient _fleet;

    public PushFileExecutor(ILogger<PushFileExecutor> log, FleetClient fleet)
    {
        _log = log;
        _fleet = fleet;
    }

    public string Type => "push_file";

    public async Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput,
                                                  CancellationToken ct)
    {
        var transferId = cmd.Params.GetString("transfer_id") ?? "";
        var url = cmd.Params.GetString("url") ?? "";
        var destination = cmd.Params.GetString("destination") ?? "";
        var name = cmd.Params.GetString("name") ?? "";
        var overwrite = cmd.Params.GetBool("overwrite");

        if (transferId.Length == 0 || url.Length == 0)
            return CommandResult.Fail("This upload is missing its transfer details.");

        var refusal = PathRules.Reject(destination) ?? PathRules.RejectName(name);
        if (refusal is not null)
            return await FailAsync(transferId, refusal, ct);
        destination = PathRules.Normalize(destination);
        var target = Path.Combine(destination, name.Trim());

        if (File.Exists(target) && !overwrite)
            return await FailAsync(transferId, $"{name} already exists there.", ct);
        if (Directory.Exists(target))
            return await FailAsync(transferId, $"{name} is a folder on this machine.", ct);

        var staged = TransferStaging.Reserve(transferId);
        try
        {
            onOutput?.Invoke($"[files] fetching {name}");
            var error = await _fleet.DownloadFileAsync(url, staged, viaHub: true, ct);
            if (error is not null)
                return await FailAsync(transferId, error, ct);

            Directory.CreateDirectory(destination);
            // Move rather than copy, and only now: until this line the destination folder is
            // exactly as the operator left it, so an interrupted download leaves nothing
            // half-written for somebody to run.
            File.Move(staged, target, overwrite);
            var size = new FileInfo(target).Length;
            await _fleet.ReportTransferAsync(transferId, new JsonObject(), ct);
            return CommandResult.Ok($"[files] wrote {target} ({size / 1024.0 / 1024.0:F1} MB)");
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException
                                    or NotSupportedException)
        {
            _log.LogWarning(e, "push_file failed for {Target}", target);
            return await FailAsync(transferId, e.Message, ct);
        }
        finally
        {
            TransferStaging.Discard(staged);
        }
    }

    private async Task<CommandResult> FailAsync(string transferId, string reason,
                                                CancellationToken ct)
    {
        await _fleet.ReportTransferAsync(transferId, new JsonObject { ["error"] = reason }, ct);
        return CommandResult.Fail($"[files] {reason}");
    }
}
