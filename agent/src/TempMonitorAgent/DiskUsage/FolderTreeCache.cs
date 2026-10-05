using System.Text.Json.Nodes;

namespace TempMonitorAgent.DiskUsage;

/// <summary>
/// Puts folder sizes from the last disk-usage scan into directory listings (roadmap #27).
///
/// **Loaded on the first listing, dropped after <see cref="IdleLifetime"/> without one.**
/// A system drive's tree is tens of megabytes in memory. Keeping it resident around the clock
/// would cost every PC in the fleet that much for the minutes a year somebody browses it.
/// Reading it from disk takes well under a second, once per browsing session.
///
/// **Never fails a listing.** A missing, stale or unreadable tree means a listing without
/// sizes, which is what every agent before this one sent. It never turns into an error in
/// place of the folder's contents.
/// </summary>
internal static class FolderTreeCache
{
    internal static readonly TimeSpan IdleLifetime = TimeSpan.FromMinutes(10);

    private static readonly Lock Gate = new();
    private static readonly Dictionary<string, (FolderTree? Tree, DateTimeOffset LastUsed)> Trees =
        new(StringComparer.OrdinalIgnoreCase);

    /// <summary>Overridable by tests, which point it at a temp directory.</summary>
    internal static Func<string, string> TreePath { get; set; } =
        letter => Path.Combine(AgentConfig.DiskUsageDir, letter.TrimEnd(':') + ".cur");

    /// <summary>
    /// Add <c>tree_size</c>, <c>tree_allocated</c> and <c>tree_files</c> to every folder entry
    /// in <paramref name="entries"/> that the tree knows. Returns the scan time to report as
    /// <c>tree_scanned_at</c>, or null when there is no tree for this volume, so the console
    /// can tell "not scanned yet" from "scanned, and this folder is empty".
    ///
    /// **A link gets no size.** A junction's contents belong to its target, and the MFT
    /// counts them there. "Documents and Settings" showing the size of C:\Users would count
    /// those bytes twice for anyone adding up a column.
    /// </summary>
    public static long? Annotate(string folder, JsonArray entries)
    {
        try
        {
            if (folder.Length < 2 || folder[1] != ':') return null;
            var tree = Get(folder[..2]);
            if (tree is null) return null;
            var at = tree.Find(folder);
            if (at < 0) return tree.ScannedAt.ToUnixTimeSeconds();

            foreach (var node in entries)
            {
                if (node is not JsonObject entry) continue;
                if (entry["directory"]?.GetValue<bool>() != true) continue;
                if (entry["link"]?.GetValue<bool>() == true) continue;
                var name = entry["name"]?.GetValue<string>();
                if (string.IsNullOrEmpty(name)) continue;
                var child = tree.Child(at, name);
                if (child < 0) continue;
                entry["tree_size"] = tree.Size[child];
                entry["tree_allocated"] = tree.Allocated[child];
                entry["tree_files"] = tree.Files[child];
            }
            return tree.ScannedAt.ToUnixTimeSeconds();
        }
        catch (Exception e) when (e is InvalidOperationException or FormatException
                                    or IOException or UnauthorizedAccessException)
        {
            return null;
        }
    }

    /// <summary>The tree for <paramref name="letter"/> ("C:"), loading it if needed.</summary>
    internal static FolderTree? Get(string letter)
    {
        lock (Gate)
        {
            if (Trees.TryGetValue(letter, out var hit))
            {
                Trees[letter] = (hit.Tree, DateTimeOffset.UtcNow);
                return hit.Tree;
            }
        }
        // Loaded outside the lock: two listings on different volumes should not wait on each
        // other's disk read. A duplicate load of the same volume is harmless.
        // Folders only: their totals already include every file, and a browsing session has
        // no use for a million file names held in memory.
        var tree = FolderTree.Load(TreePath(letter), includeFiles: false);
        lock (Gate)
        {
            Trees[letter] = (tree, DateTimeOffset.UtcNow);
        }
        return tree;
    }

    /// <summary>Drop a volume's tree, after a scan replaced the file it came from.</summary>
    public static void Forget(string letter)
    {
        lock (Gate) { Trees.Remove(letter); }
    }

    /// <summary>Drop every tree not used for <see cref="IdleLifetime"/>. Called from the
    /// inventory loop, so no timer of its own is needed.</summary>
    public static void Trim()
    {
        lock (Gate)
        {
            var cutoff = DateTimeOffset.UtcNow - IdleLifetime;
            foreach (var key in Trees.Where(kv => kv.Value.LastUsed < cutoff).Select(kv => kv.Key).ToList())
                Trees.Remove(key);
        }
    }
}
