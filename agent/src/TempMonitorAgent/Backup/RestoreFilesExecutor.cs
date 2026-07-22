using System.Formats.Tar;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging;
using TempMonitorAgent.Fleet;
using TempMonitorAgent.Fleet.Executors;

namespace TempMonitorAgent.Backup;

/// <summary>
/// restore_files: pull selected files out of this machine's (or another machine's) backup
/// archives and write them back. Roadmap #1b, the other half of BackupFilesExecutor.
///
/// The command params are deliberately almost empty — a restore id and the size of the
/// job. Everything else is FETCHED from the hub:
///
///   * a restore names FILES, tens of thousands of them, and the hub audits command params
///     verbatim; the same shape would write a multi-megabyte audit row per restore, into
///     the database that is itself being backed up, and
///   * the decryption key would be sitting in that log with them.
///
/// Four steps:
///
///   1. **Fetch the plan** — archives, the members wanted from each, and the key.
///   2. **Download an archive** to staging (pre-signed S3 GET, or proxied by the hub for
///      WebDAV — the agent never holds the destination credential, exactly as on the way up).
///   3. **Decrypt and unpack it**, extracting only the members this restore asked for.
///   4. **Report** how many files actually landed.
///
/// One archive at a time, deleted as soon as it is unpacked: a chain can be hundreds of
/// gigabytes across its archives, and a restore that needs free space for all of them at
/// once is a restore that fails on the machine that needed it most.
///
/// **A partial restore is reported as a FAILURE**, with the counts. "Restored 900 of 1000
/// files" needs a human to look at which 100 are missing, and a green row means nobody
/// ever does.
/// </summary>
public sealed class RestoreFilesExecutor : ICommandExecutor
{
    private readonly ILogger<RestoreFilesExecutor> _log;
    private readonly FleetClient _fleet;

    public RestoreFilesExecutor(ILogger<RestoreFilesExecutor> log, FleetClient fleet)
    {
        _log = log;
        _fleet = fleet;
    }

    public string Type => "restore_files";

    private static string StagingDir => Path.Combine(AgentConfig.ProgramDataDir, "backup", "restore");

    public async Task<CommandResult> ExecuteAsync(
        FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        var restoreId = cmd.Params.GetString("restore_id");
        if (string.IsNullOrEmpty(restoreId))
            return CommandResult.Fail("restore_files params carry no restore_id");

        var log = new StringBuilder();
        void Say(string line)
        {
            log.Append(line).Append('\n');
            onOutput?.Invoke(line + "\n");
        }

        var plan = await _fleet.FetchRestorePlanAsync(restoreId, ct);
        if (plan is null)
        {
            // No ReportRestoreAsync here: either the hub does not believe this restore is
            // ours, or it is already finished. Reporting against it would be shouting at a
            // row that is not listening; the command result is where this belongs.
            return CommandResult.Fail(
                "The hub would not supply the plan for this restore. It may have already "
                + "finished, or been superseded.");
        }

        var keyB64 = plan.GetObject("encryption").GetString("key");
        if (string.IsNullOrEmpty(keyB64))
            return await FailAsync(restoreId, "The restore plan carries no decryption key.",
                                   log, Say, ct);
        byte[] key;
        try { key = Convert.FromBase64String(keyB64); }
        catch (FormatException)
        {
            return await FailAsync(restoreId, "The restore key is not valid base64.",
                                   log, Say, ct);
        }

        var targetDir = plan.GetString("target_dir") ?? "";
        bool overwrite = plan["overwrite"]?.GetValue<bool>() ?? false;
        var sourceMachine = plan.GetString("source_machine") ?? "";
        var archives = plan["archives"] as JsonArray ?? [];

        Say($"[restore] {plan["file_count"]} file(s) from {sourceMachine}, "
            + $"{archives.Count} archive(s) → "
            + (string.IsNullOrEmpty(targetDir) ? "their original locations" : targetDir));
        if (!string.IsNullOrEmpty(targetDir) && !IsAbsoluteLocalPath(targetDir))
            return await FailAsync(restoreId,
                $"The restore folder {targetDir} is not an absolute local path.",
                log, Say, ct);

        int restored = 0;
        long bytesRestored = 0;
        var failures = new List<string>();

        try
        {
            Directory.CreateDirectory(StagingDir);
            foreach (var node in archives)
            {
                ct.ThrowIfCancellationRequested();
                if (node is not JsonObject archive) continue;

                var index = archive.GetInt("index", 0);
                var wanted = WantedMembers(archive, targetDir, overwrite, failures);
                if (wanted.Count == 0)
                {
                    Say($"[restore] archive {index + 1}/{archives.Count}: nothing left to take");
                    continue;
                }

                var download = archive.GetObject("download");
                var url = download.GetString("url");
                if (string.IsNullOrEmpty(url))
                {
                    failures.Add($"archive {index}: no download URL");
                    continue;
                }

                var staged = Path.Combine(StagingDir, $"restore-{restoreId}-{index}.fhb");
                try
                {
                    Say($"[restore] archive {index + 1}/{archives.Count}: downloading "
                        + $"{wanted.Count} file(s)");
                    var error = await _fleet.DownloadBackupAsync(
                        url, staged, download.GetString("kind") == "hub", ct);
                    if (error is not null)
                    {
                        failures.Add($"archive {index}: {error}");
                        continue;
                    }

                    var (wrote, bytes) = Unpack(staged, key, wanted, failures);
                    restored += wrote;
                    bytesRestored += bytes;
                }
                finally
                {
                    // Deleted before the next archive is fetched, not at the end: see the
                    // class docstring on free space.
                    try { if (File.Exists(staged)) File.Delete(staged); }
                    catch (Exception) { /* the next run's CreateDirectory tolerates leftovers */ }
                }
            }
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (Exception e)
        {
            _log.LogWarning(e, "restore_files failed");
            return await FailAsync(restoreId, e.Message, log, Say, ct);
        }

        foreach (var failure in failures.Take(10)) Say($"[restore] problem: {failure}");
        Say($"[restore] {restored} file(s), {bytesRestored / 1024.0 / 1024:F1} MB written");

        var report = new JsonObject
        {
            ["restored"] = restored,
            ["bytes_restored"] = bytesRestored,
            ["failures"] = new JsonArray(failures.Take(20)
                .Select(f => (JsonNode)JsonValue.Create(f)!).ToArray()),
        };
        if (!await _fleet.ReportRestoreAsync(restoreId, report, ct))
            return new CommandResult(false, log + "\n[restore] the files were written but "
                                                + "the result could not be reported.");
        // The hub decides success from the counts (it knows how many were asked for), but
        // the COMMAND still has to say something -- and a run with unwritten files is not
        // a green command either.
        return failures.Count == 0
            ? CommandResult.Ok(log.ToString())
            : new CommandResult(false, log.ToString());
    }

    /// <summary>
    /// Which members to take from one archive, and where each one goes.
    ///
    /// Resolved BEFORE the download so an archive whose files all already exist (and
    /// overwrite is off) is never fetched at all — on a re-run of a partly-failed restore
    /// that is most of them, and each one is gigabytes.
    /// </summary>
    private static Dictionary<string, string> WantedMembers(
        JsonObject archive, string targetDir, bool overwrite, List<string> failures)
    {
        var wanted = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (var node in archive["files"] as JsonArray ?? [])
        {
            if (node is not JsonObject file) continue;
            var member = file.GetString("member");
            var original = file.GetString("path");
            if (string.IsNullOrEmpty(member) || string.IsNullOrEmpty(original)) continue;

            var target = ResolveTarget(member, original, targetDir);
            if (target is null)
            {
                failures.Add($"{original}: refused (unsafe path)");
                continue;
            }
            if (!overwrite && File.Exists(target))
            {
                failures.Add($"{original}: already exists");
                continue;
            }
            wanted[member] = target;
        }
        return wanted;
    }

    /// <summary>
    /// Where one member is written, or null if it would escape.
    ///
    /// Restoring to the ORIGINAL location uses the manifest's absolute path, which the hub
    /// took from this machine's own walk. Restoring into a folder rebuilds the tree under
    /// it from the MEMBER name (<c>C/Users/bob/x.txt</c> → <c>&lt;dir&gt;\C\Users\bob\x.txt</c>)
    /// so two files with the same name from different folders cannot collide.
    ///
    /// Either way the result is checked to be inside where it belongs. A backup archive is
    /// a file that came back from a machine over the network, and a member named
    /// <c>../../Windows/System32/</c> writing outside the restore folder is the classic tar
    /// traversal — refused, never sanitised into something that looks close enough.
    /// </summary>
    public static string? ResolveTarget(string member, string originalPath, string targetDir)
    {
        if (string.IsNullOrEmpty(targetDir))
        {
            if (!IsAbsoluteLocalPath(originalPath)) return null;
            // A ".." SEGMENT, not the substring: `report..final.txt` is a perfectly
            // ordinary filename, and refusing it would quietly drop real files from a
            // restore for no security gain.
            if (originalPath.Split('\\', '/').Any(p => p == "..")) return null;
            return originalPath;
        }

        var parts = member.Replace('\\', '/').Split('/', StringSplitOptions.RemoveEmptyEntries);
        if (parts.Length == 0 || parts.Any(p => p == ".." || p == ".")) return null;
        if (parts[0].Contains(':')) return null;

        var root = Path.GetFullPath(targetDir);
        var full = Path.GetFullPath(Path.Combine(root, Path.Combine(parts)));
        var prefix = root.EndsWith(Path.DirectorySeparatorChar) ? root
                                                                : root + Path.DirectorySeparatorChar;
        return full.StartsWith(prefix, StringComparison.OrdinalIgnoreCase) ? full : null;
    }

    private static bool IsAbsoluteLocalPath(string path) =>
        path.Length >= 3 && char.IsLetter(path[0]) && path[1] == ':'
        && (path[2] == '\\' || path[2] == '/');

    /// <summary>
    /// Decrypt one archive and write out the members this restore wants.
    ///
    /// Streamed: the envelope is decrypted chunk by chunk into a gzip reader into a tar
    /// reader, so a 40 GB archive costs a buffer rather than 40 GB of scratch — the same
    /// property the backup side insists on, in the other direction.
    /// </summary>
    public static (int Files, long Bytes) Unpack(string archivePath, byte[] key,
                                                 Dictionary<string, string> wanted,
                                                 List<string> failures)
    {
        int written = 0;
        long bytes = 0;
        var remaining = new Dictionary<string, string>(wanted, StringComparer.OrdinalIgnoreCase);

        using var file = new FileStream(archivePath, FileMode.Open, FileAccess.Read,
                                        FileShare.Read, 1024 * 1024);
        Stream plaintext;
        try
        {
            plaintext = BackupEnvelope.Open(file, key).Plaintext;
        }
        catch (Exception e) when (e is InvalidDataException or CryptographicException)
        {
            failures.Add($"archive could not be decrypted: {e.Message}");
            return (0, 0);
        }

        using (plaintext)
        using (var gzip = new System.IO.Compression.GZipStream(
                   plaintext, System.IO.Compression.CompressionMode.Decompress))
        using (var tar = new TarReader(gzip))
        {
            TarEntry? entry;
            while ((entry = tar.GetNextEntry(copyData: false)) is not null)
            {
                if (remaining.Count == 0) break;    // everything wanted is out
                if (entry.EntryType is not (TarEntryType.RegularFile or TarEntryType.V7RegularFile))
                    continue;
                if (!remaining.TryGetValue(entry.Name, out var target)) continue;
                remaining.Remove(entry.Name);

                try
                {
                    Directory.CreateDirectory(Path.GetDirectoryName(target)!);
                    // Written beside the target and renamed: a decrypt failure or a dropped
                    // connection mid-entry would otherwise leave a truncated file sitting at
                    // exactly the name someone is restoring BECAUSE the original was bad.
                    var partial = target + ".fhrestore";
                    using (var output = new FileStream(partial, FileMode.Create, FileAccess.Write,
                                                       FileShare.None, 1024 * 1024))
                    {
                        entry.DataStream?.CopyTo(output);
                        bytes += output.Length;
                    }
                    File.Move(partial, target, overwrite: true);
                    if (entry.ModificationTime != default)
                        File.SetLastWriteTimeUtc(target, entry.ModificationTime.UtcDateTime);
                    written++;
                }
                catch (Exception e)
                {
                    failures.Add($"{entry.Name}: {e.Message}");
                }
            }
        }

        // Named rather than silently counted short: a member the manifest promised and the
        // archive does not hold means the two disagree, and that is worth an operator's
        // attention even when everything else restored.
        foreach (var missing in remaining.Keys.Take(10))
            failures.Add($"{missing}: not present in the archive");
        return (written, bytes);
    }

    private async Task<CommandResult> FailAsync(string restoreId, string reason,
                                                StringBuilder log, Action<string> say,
                                                CancellationToken ct)
    {
        say($"[restore] FAILED: {reason}");
        await _fleet.ReportRestoreAsync(restoreId, new JsonObject { ["error"] = reason }, ct);
        return new CommandResult(false, log.ToString());
    }
}
