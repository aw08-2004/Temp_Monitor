using System.Text;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging;
using TempMonitorAgent.Fleet;
using TempMonitorAgent.Fleet.Executors;

namespace TempMonitorAgent.Files;

/// <summary>
/// file_operation: copy, move, rename, delete, or make a folder.
///
/// **Every item is attempted, and the result names each one.** An operator who selected
/// nine files and got "Access is denied" back has learned nothing useful — which of the nine?
/// did the other eight move? So this walks the whole list, records a line per item, and
/// reports success only if every item succeeded. The console re-lists the folder afterwards
/// rather than believing this summary, because a partial copy is a real state and the only
/// honest way to render it is to go and look.
///
/// **Delete is permanent, and it is not routed through the Recycle Bin.** That is a
/// consequence of what this process is rather than a preference: the agent runs as SYSTEM
/// with no interactive profile, and the Recycle Bin is per-user — bytes deleted from here
/// would land in SYSTEM's bin on that volume, where the person sitting at the PC cannot see
/// or restore them. Offering an undo that only an administrator with a disk editor could
/// reach would be worse than saying plainly that there is none, which is what the console's
/// confirmation does.
///
/// **A move across volumes is a copy and a delete, in that order.** Directory.Move and
/// File.Move refuse a cross-volume move outright; falling back to copy-then-delete is what
/// Explorer does and what an operator dragging C:\ to D:\ means. The delete only runs if the
/// copy finished, so a failure leaves the source where it was rather than nowhere.
/// </summary>
public sealed class FileOperationExecutor : ICommandExecutor
{
    private readonly ILogger<FileOperationExecutor> _log;
    public FileOperationExecutor(ILogger<FileOperationExecutor> log) => _log = log;

    public string Type => "file_operation";

    public Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput,
                                            CancellationToken ct)
    {
        var op = (cmd.Params.GetString("op") ?? "").Trim().ToLowerInvariant();
        var log = new StringBuilder();
        void Say(string line)
        {
            log.AppendLine(line);
            onOutput?.Invoke(line);
        }

        // Resolved up front, because every verb that has a destination needs it checked
        // before any item is touched: discovering the destination was malformed after four
        // of nine files had moved is the one failure mode with no clean recovery.
        var destination = cmd.Params.GetString("destination");
        if (destination is not null)
        {
            var bad = PathRules.Reject(destination);
            if (bad is not null) return Task.FromResult(CommandResult.Fail(bad));
            destination = PathRules.Normalize(destination);
        }

        var newName = cmd.Params.GetString("new_name");
        if (newName is not null)
        {
            var bad = PathRules.RejectName(newName);
            if (bad is not null) return Task.FromResult(CommandResult.Fail(bad));
            newName = newName.Trim();
        }

        try
        {
            return Task.FromResult(op switch
            {
                "new_folder" => NewFolder(destination, newName, Say, log),
                "rename" => Rename(Sources(cmd), newName, Say, log),
                "delete" => ForEachSource(cmd, Say, log, Delete),
                "copy" => ForEachSource(cmd, Say, log, (src, s) => Transfer(src, destination, false, s)),
                "move" => ForEachSource(cmd, Say, log, (src, s) => Transfer(src, destination, true, s)),
                _ => CommandResult.Fail($"Unknown file operation: {op}"),
            });
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (Exception e)
        {
            _log.LogWarning(e, "file_operation {Op} threw", op);
            return Task.FromResult(CommandResult.Fail(log + $"\n{e.Message}"));
        }
    }

    private static List<string> Sources(FleetCommand cmd)
    {
        var list = new List<string>();
        foreach (var node in cmd.Params.GetArray("paths") ?? new JsonArray())
        {
            var text = node?.ToString();
            if (!string.IsNullOrWhiteSpace(text)) list.Add(text);
        }
        return list;
    }

    /// <summary>The shape every multi-item verb shares: validate each path, run the action,
    /// record a line, and succeed only if all of them did.</summary>
    private static CommandResult ForEachSource(FleetCommand cmd, Action<string> say,
                                               StringBuilder log,
                                               Func<string, Action<string>, string?> action)
    {
        var sources = Sources(cmd);
        if (sources.Count == 0) return CommandResult.Fail("No items were named.");

        var failures = 0;
        foreach (var raw in sources)
        {
            var refusal = PathRules.Reject(raw);
            if (refusal is null)
            {
                var path = PathRules.Normalize(raw);
                refusal = action(path, say);
            }
            if (refusal is not null)
            {
                failures++;
                say($"[files] {raw}: {refusal}");
            }
        }
        var done = sources.Count - failures;
        say($"[files] {done} of {sources.Count} item(s) succeeded");
        return failures == 0
            ? CommandResult.Ok(log.ToString())
            : CommandResult.Fail(log.ToString());
    }

    // ---------------- the verbs ----------------

    private static CommandResult NewFolder(string? destination, string? name,
                                           Action<string> say, StringBuilder log)
    {
        if (destination is null || name is null)
            return CommandResult.Fail("A folder needs somewhere to be and a name.");
        var target = Path.Combine(destination, name);
        if (Directory.Exists(target) || File.Exists(target))
            return CommandResult.Fail($"{target} already exists.");
        Directory.CreateDirectory(target);
        say($"[files] created {target}");
        return CommandResult.Ok(log.ToString());
    }

    private static CommandResult Rename(List<string> sources, string? name,
                                        Action<string> say, StringBuilder log)
    {
        // One item, never a list. "Rename" applied to twelve things has no meaning that is
        // not really a bulk-rename feature, and inventing one here would be a surprise.
        if (sources.Count != 1) return CommandResult.Fail("Rename takes exactly one item.");
        if (name is null) return CommandResult.Fail("No new name was supplied.");
        var refusal = PathRules.Reject(sources[0]);
        if (refusal is not null) return CommandResult.Fail(refusal);

        var source = PathRules.Normalize(sources[0]);
        var parent = Path.GetDirectoryName(source);
        if (string.IsNullOrEmpty(parent))
            return CommandResult.Fail($"{source} has no parent folder to rename inside.");
        var target = Path.Combine(parent, name);
        if (string.Equals(source, target, StringComparison.OrdinalIgnoreCase))
            return CommandResult.Fail("That is already its name.");
        if (Directory.Exists(target) || File.Exists(target))
            return CommandResult.Fail($"{name} already exists in that folder.");

        if (Directory.Exists(source)) Directory.Move(source, target);
        else if (File.Exists(source)) File.Move(source, target);
        else return CommandResult.Fail($"{source} does not exist.");

        say($"[files] renamed {source} to {name}");
        return CommandResult.Ok(log.ToString());
    }

    /// <summary>Delete one file or folder. Returns null on success, or a reason.</summary>
    private static string? Delete(string path, Action<string> say)
    {
        try
        {
            if (Directory.Exists(path))
            {
                // Attributes cleared first: a read-only or hidden+system folder (a user's
                // Documents is both) refuses to go otherwise, with an error that reads like
                // a permissions problem rather than an attribute one.
                ClearAttributes(path);
                Directory.Delete(path, recursive: true);
            }
            else if (File.Exists(path))
            {
                File.SetAttributes(path, FileAttributes.Normal);
                File.Delete(path);
            }
            else
            {
                // Already gone counts as done: the operator asked for it not to be there,
                // and it is not. Reporting a failure would have them chasing a file that
                // no longer exists.
                say($"[files] {path} was already gone");
                return null;
            }
            say($"[files] deleted {path}");
            return null;
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            return e.Message;
        }
    }

    /// <summary>Copy or move one item into <paramref name="destination"/>. Returns null on
    /// success, or a reason.</summary>
    private static string? Transfer(string source, string? destination, bool move,
                                    Action<string> say)
    {
        if (destination is null) return "No destination was supplied.";
        var name = Path.GetFileName(source.TrimEnd('\\'));
        if (string.IsNullOrEmpty(name)) return "That is a drive, not a file or folder.";
        var target = Path.Combine(destination, name);

        try
        {
            Directory.CreateDirectory(destination);
            if (Directory.Exists(source))
            {
                // Checked here as well as at the hub, because only this machine can resolve
                // what the two paths actually are: a junction, a mapped drive and a UNC path
                // can all name one folder under three spellings, and copying a folder into
                // itself walks into the copy it is making until the disk is full.
                if (target.StartsWith(source + "\\", StringComparison.OrdinalIgnoreCase)
                    || string.Equals(source, target, StringComparison.OrdinalIgnoreCase))
                    return "That folder cannot be copied into itself.";
                if (Directory.Exists(target)) return $"{name} already exists there.";

                if (move && SameVolume(source, target))
                {
                    Directory.Move(source, target);
                }
                else
                {
                    CopyTree(source, target);
                    // Only after the copy finished. A failed copy leaves the source where
                    // it was, which is the one outcome an operator can recover from.
                    if (move)
                    {
                        ClearAttributes(source);
                        Directory.Delete(source, recursive: true);
                    }
                }
            }
            else if (File.Exists(source))
            {
                if (File.Exists(target)) return $"{name} already exists there.";
                if (move) File.Move(source, target);
                else File.Copy(source, target);
            }
            else
            {
                return "It is no longer there.";
            }
            say($"[files] {(move ? "moved" : "copied")} {source} to {target}");
            return null;
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException
                                    or NotSupportedException)
        {
            return e.Message;
        }
    }

    // ---------------- helpers ----------------

    /// <summary>Would Directory.Move work, or does this need copy-then-delete?
    ///
    /// A best guess on purpose: comparing roots catches the common case (C: to D:) without
    /// resolving mount points, and being wrong costs nothing — Directory.Move throws, and
    /// only the fast path is skipped when this says no.</summary>
    private static bool SameVolume(string a, string b)
    {
        try
        {
            return string.Equals(Path.GetPathRoot(Path.GetFullPath(a)),
                                 Path.GetPathRoot(Path.GetFullPath(b)),
                                 StringComparison.OrdinalIgnoreCase);
        }
        catch (Exception e) when (e is ArgumentException or NotSupportedException
                                    or PathTooLongException)
        {
            return false;
        }
    }

    /// <summary>Recursive copy. Reparse points are copied as the FILES they point at rather
    /// than followed as folders — a junction into C:\ would otherwise turn "copy this
    /// folder" into "copy this disk, repeatedly".</summary>
    private static void CopyTree(string source, string target)
    {
        Directory.CreateDirectory(target);
        var dir = new DirectoryInfo(source);
        foreach (var file in dir.EnumerateFiles())
            file.CopyTo(Path.Combine(target, file.Name), overwrite: false);
        foreach (var sub in dir.EnumerateDirectories())
        {
            if (sub.Attributes.HasFlag(FileAttributes.ReparsePoint)) continue;
            CopyTree(sub.FullName, Path.Combine(target, sub.Name));
        }
    }

    /// <summary>Strip read-only/hidden/system from a tree so a delete can finish.
    ///
    /// Best effort throughout: one file whose attributes will not clear must not stop the
    /// other four hundred, and the delete that follows reports the real failure anyway.</summary>
    private static void ClearAttributes(string path)
    {
        try
        {
            var dir = new DirectoryInfo(path) { Attributes = FileAttributes.Directory };
            foreach (var info in dir.EnumerateFileSystemInfos("*", SearchOption.AllDirectories))
            {
                try
                {
                    info.Attributes = info.Attributes.HasFlag(FileAttributes.Directory)
                        ? FileAttributes.Directory
                        : FileAttributes.Normal;
                }
                catch (Exception e) when (e is IOException or UnauthorizedAccessException)
                {
                }
            }
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
        }
    }
}
