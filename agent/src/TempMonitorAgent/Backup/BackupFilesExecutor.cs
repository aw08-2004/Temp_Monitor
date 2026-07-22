using System.Formats.Tar;
using System.IO.Compression;
using System.IO.Pipes;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging;
using TempMonitorAgent.Fleet;
using TempMonitorAgent.Fleet.Executors;

namespace TempMonitorAgent.Backup;

/// <summary>
/// backup_files: capture this machine's configured folders and upload them. Roadmap #1b.
///
/// The params are a SNAPSHOT of the backup policy taken when the run was dispatched —
/// include/exclude patterns, limits, the object key, a one-shot upload target, and THIS
/// machine's derived encryption key. Not a pointer to the policy, for the same reason
/// deploy_package snapshots its recipe: an operator editing the include list mid-run must
/// not give one machine a half-old definition of what was backed up, because the manifest
/// recorded afterwards would then describe neither.
///
/// Six steps:
///
///   1. **Expand the patterns** against this machine's real profiles (PathExpander), so
///      %Users% and %Desktop% become actual folders — following OneDrive redirection.
///   2. **Snapshot the volumes** (VssSnapshot) so open files are captured. Failure here is
///      skip-and-report, never fail-the-run.
///   3. **Walk and select**: apply excludes, size caps, and — for an incremental — the
///      local manifest cache, so unchanged files are skipped.
///   4. **Pack** into tar → gzip → AES-256-GCM (BackupEnvelope), to a temp file.
///   5. **Upload** to the pre-signed S3 URL, or to the hub for a WebDAV destination.
///   6. **Report** the manifest so the hub knows what the archive contains.
///
/// The archive is built to a temp file before the upload starts rather than streamed
/// straight to the socket: a mid-stream failure would otherwise leave a partial object
/// that looks exactly like a good one to the next rotation pass. Local temp is cheap; a
/// rotation that keeps a truncated generation is not.
///
/// **Tar is deliberate.** restore_backup.py unpacks it with stdlib tarfile, so a machine's
/// backup stays recoverable with no hub and no agent — the same property the hub-database
/// backup insisted on.
/// </summary>
public sealed class BackupFilesExecutor : ICommandExecutor
{
    private readonly ILogger<BackupFilesExecutor> _log;
    private readonly FleetClient _fleet;

    public BackupFilesExecutor(ILogger<BackupFilesExecutor> log, FleetClient fleet)
    {
        _log = log;
        _fleet = fleet;
    }

    public string Type => "backup_files";

    private static string StagingDir => Path.Combine(AgentConfig.ProgramDataDir, "backup", "staging");

    public async Task<CommandResult> ExecuteAsync(
        FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        var runId = cmd.Params.GetString("run_id");
        var chainId = cmd.Params.GetString("chain_id");
        var objectKey = cmd.Params.GetString("object_key");
        if (string.IsNullOrEmpty(runId) || string.IsNullOrEmpty(chainId)
            || string.IsNullOrEmpty(objectKey))
            return CommandResult.Fail("backup_files params are missing run_id/chain_id/object_key");

        var sequence = cmd.Params.GetInt("sequence", 0);
        bool wantFull = (cmd.Params as JsonObject)?["full"]?.GetValue<bool>() ?? true;
        var includes = StringList(cmd.Params, "include");
        var excludes = StringList(cmd.Params, "exclude");
        var limits = cmd.Params.GetObject("limits");
        long maxFileBytes = (long)limits.GetInt("max_file_mb", 2048) * 1024 * 1024;
        long maxSetBytes = (long)limits.GetInt("max_set_gb", 100) * 1024 * 1024 * 1024;
        bool useVss = limits?["use_vss"]?.GetValue<bool>() ?? true;

        var encryption = cmd.Params.GetObject("encryption");
        var keyB64 = encryption.GetString("key");
        if (string.IsNullOrEmpty(keyB64))
            return CommandResult.Fail("backup_files params carry no encryption key");
        byte[] key;
        try { key = Convert.FromBase64String(keyB64); }
        catch (FormatException) { return CommandResult.Fail("backup_files encryption key is not valid base64"); }

        var log = new StringBuilder();
        void Say(string line)
        {
            log.Append(line).Append('\n');
            onOutput?.Invoke(line + "\n");
        }

        if (includes.Count == 0)
            return await FailRunAsync(runId, "No paths are configured for backup.", log, Say, ct);

        var archivePath = Path.Combine(StagingDir, $"backup-{runId}.fhb");
        try
        {
            Directory.CreateDirectory(StagingDir);

            // ---- 1. expand ----
            var profiles = PathExpander.Discover();
            var problems = new List<string>();
            var roots = new List<string>();
            foreach (var pattern in includes)
            {
                foreach (var path in PathExpander.Expand(pattern, profiles, problems))
                    if (!roots.Contains(path, StringComparer.OrdinalIgnoreCase)) roots.Add(path);
            }
            Say($"[backup] {roots.Count} folder(s) from {includes.Count} pattern(s)");
            foreach (var problem in problems) Say($"[backup] note: {problem}");
            if (roots.Count == 0)
                return await FailRunAsync(runId,
                    "The configured paths resolve to nothing on this machine. "
                    + string.Join(" ", problems), log, Say, ct);

            var matcher = new PathExpander.ExcludeMatcher(excludes, profiles);

            // ---- 2. snapshot ----
            using var snapshot = new VssSnapshot(_log);
            if (useVss)
            {
                foreach (var root in roots) snapshot.EnsureVolume(root);
                foreach (var problem in snapshot.Problems) Say($"[backup] VSS: {problem}");
                if (snapshot.SnapshottedVolumes.Count > 0)
                    Say($"[backup] shadow copy of {string.Join(", ", snapshot.SnapshottedVolumes)}");
            }
            else
            {
                Say("[backup] shadow copies disabled by policy; open files may be skipped");
            }

            // ---- 3. select ----
            var cache = wantFull ? null : BackupManifest.Load(chainId);
            bool full = wantFull || cache is null;
            if (!wantFull && cache is null)
            {
                // Self-healing rather than silently wrong -- see BackupManifest.
                Say("[backup] no local manifest for this chain; taking a FULL backup instead "
                    + "of an incremental");
                full = true;
            }

            var selection = Select(roots, matcher, snapshot, cache, full,
                                   maxFileBytes, maxSetBytes, Say, ct);
            if (selection.Aborted is not null)
                return await FailRunAsync(runId, selection.Aborted, log, Say, ct);

            Say($"[backup] {selection.Files.Count} file(s), {Mb(selection.TotalBytes)} to upload"
                + (full ? " (full)" : " (incremental)")
                + (selection.Skipped > 0 ? $"; {selection.Skipped} skipped" : ""));

            // ---- 4. pack ----
            var header = new JsonObject
            {
                ["kind"] = "machine_files",
                ["machine"] = cmd.Params.GetString("machine") ?? AgentConfig.MachineName,
                ["chain_id"] = chainId,
                ["sequence"] = sequence,
                ["full"] = full,
                ["agent_version"] = AgentConfig.Version,
            };

            long storedBytes;
            using (var destination = new FileStream(archivePath, FileMode.Create, FileAccess.Write,
                                                    FileShare.None, 1024 * 1024))
            using (var pipe = new TarGzipPipe(selection.Files, snapshot, _log))
            {
                (storedBytes, _) = BackupEnvelope.Write(pipe, destination, key, header);
            }
            Say($"[backup] archive {Mb(storedBytes)} encrypted");

            // ---- 5. upload ----
            var upload = cmd.Params.GetObject("upload");
            var uploadKind = upload.GetString("kind") ?? "s3";
            var uploadUrl = upload.GetString("url");
            if (string.IsNullOrEmpty(uploadUrl))
                return await FailRunAsync(runId, "No upload URL was supplied.", log, Say, ct);

            var uploadError = await _fleet.UploadBackupAsync(uploadUrl, archivePath,
                                                            uploadKind == "hub", ct);
            if (uploadError is not null)
                return await FailRunAsync(runId, uploadError, log, Say, ct);
            Say($"[backup] uploaded to {objectKey}");

            // ---- 6. report ----
            var manifest = selection.Files.Concat(selection.Deletions).ToList();
            var reported = await _fleet.ReportBackupAsync(runId, new JsonObject
            {
                ["stored_bytes"] = storedBytes,
                ["files"] = new JsonArray(manifest.Select(ToNode).ToArray()),
            }, ct);
            if (!reported)
                return new CommandResult(false, log + "\n[backup] FAILED: the archive uploaded "
                                                    + "but the manifest could not be reported.");

            SaveCache(chainId, sequence, cache, selection, full);
            Say("[backup] done");
            return CommandResult.Ok(log.ToString());
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (Exception e)
        {
            _log.LogWarning(e, "backup_files failed");
            return await FailRunAsync(runId, e.Message, log, Say, ct);
        }
        finally
        {
            try { if (File.Exists(archivePath)) File.Delete(archivePath); }
            catch (Exception) { /* staging is cleaned on the next run too */ }
        }
    }

    /// <summary>
    /// Tell the hub the run failed, so it closes the run row.
    ///
    /// This matters more than it looks: due-ness is anchored on the last ATTEMPT, and a
    /// run left open is eventually expired by the hub — but until then the machine is not
    /// due and would not be retried. Reporting the failure ourselves means the operator
    /// sees the real reason instead of "never reported a result" a day later.
    /// </summary>
    private async Task<CommandResult> FailRunAsync(string runId, string reason,
                                                   StringBuilder log, Action<string> say,
                                                   CancellationToken ct)
    {
        say($"[backup] FAILED: {reason}");
        await _fleet.ReportBackupAsync(runId, new JsonObject { ["error"] = reason }, ct);
        return new CommandResult(false, log.ToString());
    }

    private sealed record Selection(
        List<ManifestEntry> Files, List<ManifestEntry> Deletions, long TotalBytes,
        int Skipped, string? Aborted);

    /// <summary>
    /// Walk the roots and decide what goes in the archive.
    ///
    /// Reparse points are never followed. A user profile contains junctions that point at
    /// their own ancestors ("Application Data" -> AppData\Roaming, and friends, kept for
    /// XP-era compatibility), so following them is an infinite walk that fills the disk —
    /// not a hypothetical, it is the first thing that happens on a real profile.
    /// </summary>
    private Selection Select(List<string> roots, PathExpander.ExcludeMatcher matcher,
                             VssSnapshot snapshot, BackupManifest? cache, bool full,
                             long maxFileBytes, long maxSetBytes, Action<string> say,
                             CancellationToken ct)
    {
        var files = new List<ManifestEntry>();
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        long total = 0;
        int skipped = 0;

        foreach (var root in roots)
        {
            ct.ThrowIfCancellationRequested();
            var readRoot = snapshot.MapPath(root);
            if (!Directory.Exists(readRoot))
            {
                say($"[backup] note: {root} does not exist; skipping");
                continue;
            }

            foreach (var (original, readPath) in Walk(root, readRoot, matcher, say, ct))
            {
                if (!seen.Add(original)) continue;

                FileInfo info;
                try { info = new FileInfo(readPath); if (!info.Exists) continue; }
                catch (Exception) { skipped++; continue; }

                if (info.Length > maxFileBytes)
                {
                    say($"[backup] skipped (too large): {original}");
                    skipped++;
                    continue;
                }

                var mtime = new DateTimeOffset(info.LastWriteTimeUtc).ToUnixTimeSeconds();
                if (!full && cache is not null && !cache.HasChanged(original, info.Length, mtime))
                    continue;

                string hash;
                try { hash = BackupManifest.HashFile(readPath); }
                catch (Exception e)
                {
                    // A locked file with no snapshot, or one deleted mid-walk. Named and
                    // counted rather than failing the run.
                    say($"[backup] skipped (unreadable): {original} — {e.Message}");
                    skipped++;
                    continue;
                }

                total += info.Length;
                if (total > maxSetBytes)
                {
                    return new Selection(files, [], total, skipped,
                        $"This backup would exceed the {Mb(maxSetBytes)} limit. Narrow the "
                        + "included paths, add exclusions, or raise the limit deliberately.");
                }

                files.Add(new ManifestEntry
                {
                    Path = original, Size = info.Length, Mtime = mtime, Sha256 = hash,
                });
            }
        }

        // Files the chain knows about that are no longer on disk. Recorded as deletions so
        // a restore does not resurrect something the user deleted on purpose.
        var deletions = new List<ManifestEntry>();
        if (!full && cache is not null)
        {
            foreach (var (path, entry) in cache.Files)
            {
                if (entry.Deleted || seen.Contains(path)) continue;
                if (File.Exists(snapshot.MapPath(path))) continue;
                deletions.Add(new ManifestEntry { Path = path, Deleted = true });
            }
            if (deletions.Count > 0) say($"[backup] {deletions.Count} file(s) deleted since last run");
        }

        return new Selection(files, deletions, total, skipped, null);
    }

    /// <summary>Depth-first walk yielding (original path, path to read from).</summary>
    private static IEnumerable<(string Original, string ReadPath)> Walk(
        string originalRoot, string readRoot, PathExpander.ExcludeMatcher matcher,
        Action<string> say, CancellationToken ct)
    {
        var stack = new Stack<(string Original, string Read)>();
        stack.Push((originalRoot, readRoot));

        while (stack.Count > 0)
        {
            ct.ThrowIfCancellationRequested();
            var (original, read) = stack.Pop();

            IEnumerable<string> entries;
            try { entries = Directory.EnumerateFileSystemEntries(read); }
            catch (Exception e)
            {
                say($"[backup] skipped (unreadable folder): {original} — {e.Message}");
                continue;
            }

            foreach (var entry in entries)
            {
                var name = Path.GetFileName(entry);
                var childOriginal = Path.Combine(original, name);
                if (matcher.Matches(childOriginal)) continue;

                FileAttributes attributes;
                try { attributes = File.GetAttributes(entry); }
                catch (Exception) { continue; }

                // Junctions and symlinks: never followed. See Select's docstring.
                if (attributes.HasFlag(FileAttributes.ReparsePoint)) continue;

                if (attributes.HasFlag(FileAttributes.Directory))
                    stack.Push((childOriginal, entry));
                else
                    yield return (childOriginal, entry);
            }
        }
    }

    private void SaveCache(string chainId, int sequence, BackupManifest? cache,
                           Selection selection, bool full)
    {
        var manifest = (full || cache is null)
            ? new BackupManifest { ChainId = chainId }
            : cache;
        manifest.Sequence = sequence;
        if (full) manifest.Files.Clear();
        foreach (var entry in selection.Files) manifest.Files[entry.Path] = entry;
        foreach (var entry in selection.Deletions) manifest.Files.Remove(entry.Path);
        manifest.Save();
        BackupManifest.PruneOthers(chainId);
    }

    private static JsonNode ToNode(ManifestEntry entry) => new JsonObject
    {
        ["path"] = entry.Path,
        ["size"] = entry.Size,
        ["mtime"] = entry.Mtime,
        ["sha256"] = entry.Sha256,
        ["deleted"] = entry.Deleted,
    };

    private static List<string> StringList(JsonNode? node, string key)
    {
        var list = new List<string>();
        if (node is JsonObject obj && obj.TryGetPropertyValue(key, out var value)
            && value is JsonArray array)
        {
            foreach (var item in array)
            {
                var text = item?.GetValue<string>();
                if (!string.IsNullOrWhiteSpace(text)) list.Add(text);
            }
        }
        return list;
    }

    private static string Mb(long bytes) =>
        bytes >= 1024L * 1024 * 1024
            ? $"{bytes / 1024.0 / 1024 / 1024:F2} GB"
            : $"{bytes / 1024.0 / 1024:F1} MB";
}

/// <summary>
/// Streams the selected files as tar → gzip, on demand, as the envelope pulls from it.
///
/// A pipe rather than a temp tarball: a profile's worth of documents would otherwise be
/// written to disk twice (once as tar.gz, once as the encrypted archive) before anything
/// is uploaded. The producer runs on a background task and the envelope reads the other
/// end, so peak disk is one archive rather than two.
/// </summary>
internal sealed class TarGzipPipe : Stream
{
    private readonly AnonymousPipeServerStream _read;
    private readonly Task _producer;

    public TarGzipPipe(List<ManifestEntry> files, VssSnapshot snapshot, ILogger log)
    {
        _read = new AnonymousPipeServerStream(PipeDirection.In, HandleInheritability.None);
        var client = new AnonymousPipeClientStream(PipeDirection.Out, _read.ClientSafePipeHandle);

        _producer = Task.Run(() =>
        {
            try
            {
                using (client)
                using (var gzip = new GZipStream(client, CompressionLevel.Optimal, leaveOpen: true))
                using (var tar = new TarWriter(gzip, TarEntryFormat.Pax, leaveOpen: true))
                {
                    // The manifest goes in first so restore_backup.py can list an archive's
                    // contents without unpacking it.
                    WriteManifestEntry(tar, files);
                    foreach (var entry in files)
                    {
                        var readPath = snapshot.MapPath(entry.Path);
                        try
                        {
                            using var source = new FileStream(
                                readPath, FileMode.Open, FileAccess.Read,
                                FileShare.ReadWrite | FileShare.Delete, 1024 * 1024);
                            // Stored under its ORIGINAL path (drive colon stripped so tar
                            // stays portable), which is what a restore maps back from. The
                            // mapping lives in BackupManifest because the HUB implements it
                            // too, to name members in a restore plan it never wrote.
                            var name = BackupManifest.ArchiveMember(entry.Path);
                            var tarEntry = new PaxTarEntry(TarEntryType.RegularFile, name)
                            {
                                DataStream = source,
                                ModificationTime = DateTimeOffset.FromUnixTimeSeconds(entry.Mtime),
                            };
                            tar.WriteEntry(tarEntry);
                        }
                        catch (Exception e)
                        {
                            // Vanished or locked between selection and packing. The hub's
                            // manifest will name a file the archive lacks; a restore of it
                            // fails loudly rather than the whole run failing here.
                            log.LogWarning("Backup could not pack {Path}: {Msg}", entry.Path, e.Message);
                        }
                    }
                }
            }
            catch (Exception e)
            {
                log.LogWarning(e, "Backup archive producer failed");
            }
        });
    }

    private static void WriteManifestEntry(TarWriter tar, List<ManifestEntry> files)
    {
        var json = JsonSerializer.SerializeToUtf8Bytes(new { files });
        using var stream = new MemoryStream(json);
        tar.WriteEntry(new PaxTarEntry(TarEntryType.RegularFile, "manifest.json")
        {
            DataStream = stream,
        });
    }

    public override int Read(byte[] buffer, int offset, int count)
        => _read.Read(buffer, offset, count);

    public override int Read(Span<byte> buffer) => _read.Read(buffer);

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            try { _producer.Wait(TimeSpan.FromMinutes(5)); } catch (Exception) { }
            _read.Dispose();
        }
        base.Dispose(disposing);
    }

    public override bool CanRead => true;
    public override bool CanSeek => false;
    public override bool CanWrite => false;
    public override long Length => throw new NotSupportedException();
    public override long Position { get => throw new NotSupportedException(); set => throw new NotSupportedException(); }
    public override void Flush() { }
    public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
    public override void SetLength(long value) => throw new NotSupportedException();
    public override void Write(byte[] buffer, int offset, int count) => throw new NotSupportedException();
}
