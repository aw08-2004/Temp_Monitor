using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace TempMonitorAgent.Backup;

/// <summary>One file version, as recorded in a manifest.</summary>
public sealed class ManifestEntry
{
    [JsonPropertyName("path")] public string Path { get; set; } = "";
    [JsonPropertyName("size")] public long Size { get; set; }
    [JsonPropertyName("mtime")] public long Mtime { get; set; }
    [JsonPropertyName("sha256")] public string Sha256 { get; set; } = "";
    [JsonPropertyName("deleted")] public bool Deleted { get; set; }
}

/// <summary>
/// The agent's local record of what a chain already contains, so an incremental can skip
/// unchanged files. Roadmap #1b.
///
/// **Why local rather than sent by the hub.** A user profile is commonly 100k–500k files;
/// shipping that manifest down on every scheduled run would dwarf the backup itself on a
/// machine where nothing changed. The hub sends only the chain id and the sequence it
/// expects, and this file — one per chain, under %ProgramData% — supplies the rest.
///
/// **A missing or mismatched cache forces a full, and says so.** An agent that was
/// reinstalled, or whose %ProgramData% was wiped, cannot know what the previous archives
/// held; uploading an "incremental" against a chain it cannot see would produce a manifest
/// full of holes that the hub would happily record and nobody would notice until a
/// restore. Falling back to a full costs one expensive run and is always correct.
/// </summary>
public sealed class BackupManifest
{
    [JsonPropertyName("chain_id")] public string ChainId { get; set; } = "";
    [JsonPropertyName("sequence")] public int Sequence { get; set; }
    [JsonPropertyName("files")] public Dictionary<string, ManifestEntry> Files { get; set; } =
        new(StringComparer.OrdinalIgnoreCase);

    private static string Dir => System.IO.Path.Combine(AgentConfig.ProgramDataDir, "backup");

    private static string PathFor(string chainId) =>
        System.IO.Path.Combine(Dir, SafeName(chainId) + ".json");

    /// <summary>Chain ids are hub-generated hex, but this is a filename built from a
    /// value that arrives over the wire — so it is constrained rather than trusted.</summary>
    private static string SafeName(string chainId) =>
        new(chainId.Where(char.IsLetterOrDigit).Take(64).ToArray());

    /// <summary>Load the cache for a chain, or null if there isn't a usable one.</summary>
    public static BackupManifest? Load(string chainId)
    {
        try
        {
            var path = PathFor(chainId);
            if (!File.Exists(path)) return null;
            var loaded = JsonSerializer.Deserialize<BackupManifest>(File.ReadAllText(path));
            if (loaded is null || !string.Equals(loaded.ChainId, chainId, StringComparison.Ordinal))
                return null;
            // Deserialization rebuilds the dictionary with the default comparer; paths are
            // compared case-insensitively everywhere else, so restore that here.
            loaded.Files = new Dictionary<string, ManifestEntry>(
                loaded.Files, StringComparer.OrdinalIgnoreCase);
            return loaded;
        }
        catch (Exception)
        {
            // Corrupt cache == no cache. The caller forces a full, which is correct and
            // self-healing; trying to salvage a half-parsed manifest is how you upload an
            // incremental with holes in it.
            return null;
        }
    }

    public void Save()
    {
        Directory.CreateDirectory(Dir);
        var path = PathFor(ChainId);
        var temp = path + ".tmp";
        File.WriteAllText(temp, JsonSerializer.Serialize(this));
        File.Move(temp, path, overwrite: true);
    }

    /// <summary>Drop caches for chains this machine is no longer extending. Called after a
    /// successful run so a machine that has cycled through many chains does not keep a
    /// manifest per chain forever.</summary>
    public static void PruneOthers(string keepChainId)
    {
        try
        {
            if (!Directory.Exists(Dir)) return;
            var keep = PathFor(keepChainId);
            foreach (var file in Directory.EnumerateFiles(Dir, "*.json"))
            {
                if (!string.Equals(file, keep, StringComparison.OrdinalIgnoreCase))
                    File.Delete(file);
            }
        }
        catch (Exception)
        {
            // Tidiness only.
        }
    }

    /// <summary>
    /// Has this file changed since the chain last saw it?
    ///
    /// Size and write time, NOT a hash: hashing every file on every run would read the
    /// whole profile off disk nightly to discover that nothing changed. The hash is
    /// computed only for files being uploaded, and stored so a restore can verify them.
    /// A file rewritten with identical size and mtime is missed — that requires deliberate
    /// timestamp forgery, and the trade is worth several orders of magnitude of I/O.
    /// </summary>
    public bool HasChanged(string path, long size, long mtime)
    {
        if (!Files.TryGetValue(path, out var entry)) return true;
        if (entry.Deleted) return true;
        return entry.Size != size || entry.Mtime != mtime;
    }

    /// <summary>
    /// The name a file is stored under INSIDE a backup archive.
    ///
    /// <c>C:\Users\bob\Desktop\notes.txt</c> → <c>C/Users/bob/Desktop/notes.txt</c>. The
    /// drive colon goes and separators become forward slashes, because tar member names
    /// are POSIX-shaped — and the whole reason this feature packs tar is that stdlib
    /// <c>tarfile</c> can open the archive on any machine, with no hub and no agent.
    ///
    /// **A SHARED CONTRACT with the hub's backup_paths.archive_member().** The hub builds
    /// restore plans naming members it never wrote; this is what wrote them. A drift
    /// between the two is a restore that silently finds nothing, so both sides are tested
    /// against the "members" vectors in tests/backup_path_vectors.json.
    /// </summary>
    public static string ArchiveMember(string path) =>
        (path ?? "").Replace(":", "").Replace('\\', '/').TrimStart('/');

    /// <summary>
    /// The inverse, for a restore putting files back where they came from.
    ///
    /// Only the FIRST segment can have been a drive, and only when it is a single letter:
    /// a member from a UNC source (<c>srv/share/f</c>) stays relative, which is what a
    /// restore-into-a-folder wants and what a restore-to-original-location must refuse
    /// rather than guess a drive for.
    /// </summary>
    public static string MemberToPath(string member)
    {
        var text = (member ?? "").Replace('\\', '/').TrimStart('/');
        if (text.Length == 0) return "";
        var cut = text.IndexOf('/');
        var head = cut < 0 ? text : text[..cut];
        var tail = cut < 0 ? "" : text[(cut + 1)..];
        if (head.Length == 1 && char.IsLetter(head[0])) head += ":";
        // A bare drive keeps its root separator ("C:" alone means "the current directory
        // on C:", which is not what a restore means by it); anything deeper does not.
        return tail.Length == 0 ? head + "\\" : head + "\\" + tail.Replace('/', '\\');
    }

    public static string HashFile(string path)
    {
        using var stream = new FileStream(path, FileMode.Open, FileAccess.Read,
                                          FileShare.ReadWrite | FileShare.Delete,
                                          bufferSize: 1024 * 1024, useAsync: false);
        using var sha = SHA256.Create();
        return Convert.ToHexString(sha.ComputeHash(stream)).ToLowerInvariant();
    }
}
