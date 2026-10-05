using System.IO.Compression;
using System.Text;

namespace TempMonitorAgent.DiskUsage;

/// <summary>
/// Every folder and every file on one volume with their sizes, as of one scan (roadmap #27).
///
/// **Columnar, not one object per entry.** A busy system drive has a quarter of a million
/// folders and a million files. As objects that is a hundred megabytes of headers and
/// pointers; as parallel arrays it is the names plus a few dozen bytes a row. Folder index 0
/// is always the volume root.
///
/// **Two sizes, because they answer two questions.** <see cref="Size"/> is the logical
/// length, what Explorer's Properties dialog calls "Size", and what the file browser shows.
/// <see cref="Allocated"/> is what the entry actually takes on disk, after compression,
/// sparse files and tiny files living inside the MFT itself. Only the second one fills a
/// volume, so it is the one the change summary is built on.
///
/// **Every file, not only the large ones.** The hub keeps the size history of any path the
/// operator asks about, down to a single file (ROADMAP #27), so the tree the agent diffs and
/// uploads has to know every file by name. The file half is optional on load: the directory
/// listing only needs folder totals, and a browsing session should not hold a million file
/// names in a LocalSystem service to show forty folder sizes.
/// </summary>
internal sealed class FolderTree
{
    private const uint Magic = 0x55444846; // "FHDU"
    private const int FormatVersion = 2;

    public required string Volume { get; init; }
    public required DateTimeOffset ScannedAt { get; init; }
    public long TotalBytes { get; init; }
    public long FreeBytes { get; init; }

    // Folders. Parents always precede children.
    public required int[] Parent { get; init; }
    public required string[] Name { get; init; }
    public required long[] Size { get; init; }
    public required long[] Allocated { get; init; }
    public required long[] Files { get; init; }
    public required int[] SubDirs { get; init; }

    // Files, each in folder FileDir[i]. Empty when loaded without files.
    public required int[] FileDir { get; init; }
    public required string[] FileName { get; init; }
    public required long[] FileSize { get; init; }
    public required long[] FileAllocated { get; init; }

    /// <summary>False when this tree was loaded without its files.</summary>
    public bool HasFiles { get; init; } = true;

    public int Count => Parent.Length;
    public int FileCount => FileDir.Length;

    private Dictionary<(int, string), int>? _children;
    private int[]? _depth;

    /// <summary>How many levels below the root folder <paramref name="index"/> is. The root is 0.</summary>
    public int Depth(int index)
    {
        if (_depth is null)
        {
            var depth = new int[Count];
            for (var i = 1; i < Count; i++) depth[i] = depth[Parent[i]] + 1;
            _depth = depth;
        }
        return _depth[index];
    }

    /// <summary>The child folder of <paramref name="parent"/> called <paramref name="name"/>,
    /// matched as Windows matches names, or -1.</summary>
    public int Child(int parent, string name)
    {
        var map = _children ??= BuildChildMap();
        return map.TryGetValue((parent, name), out var index) ? index : -1;
    }

    /// <summary>The folder at <paramref name="path"/> (e.g. <c>C:\Users\Ann</c>), or -1.</summary>
    public int Find(string path)
    {
        var text = path.Replace('/', '\\').TrimEnd('\\');
        if (text.Length < 2 || !text[..2].Equals(Volume, StringComparison.OrdinalIgnoreCase))
            return -1;
        var index = 0;
        foreach (var part in text[2..].Split('\\', StringSplitOptions.RemoveEmptyEntries))
        {
            index = Child(index, part);
            if (index < 0) return -1;
        }
        return index;
    }

    /// <summary>The full path of folder <paramref name="index"/>, e.g. <c>C:\Users\Ann</c>.
    /// The root is <c>C:\</c>.</summary>
    public string PathOf(int index)
    {
        if (index == 0) return Volume + "\\";
        var parts = new List<string>();
        for (var i = index; i > 0; i = Parent[i]) parts.Add(Name[i]);
        parts.Reverse();
        return Volume + "\\" + string.Join('\\', parts);
    }

    /// <summary>The full path of file <paramref name="file"/>.</summary>
    public string FilePathOf(int file) => Join(PathOf(FileDir[file]), FileName[file]);

    internal static string Join(string folder, string name) =>
        folder.EndsWith('\\') ? folder + name : folder + "\\" + name;

    /// <summary>Child folders of every folder, as compressed-row arrays: the children of
    /// folder i are <c>Items[Start[i]..Start[i + 1]]</c>.</summary>
    internal (int[] Start, int[] Items) ChildIndex() =>
        Group(Count, Count - 1, i => Parent[i + 1], i => i + 1);

    /// <summary>The files of every folder, in the same shape as <see cref="ChildIndex"/>.</summary>
    internal (int[] Start, int[] Items) FileIndex() =>
        Group(Count, FileCount, i => FileDir[i], i => i);

    private static (int[] Start, int[] Items) Group(int groups, int n, Func<int, int> key,
                                                    Func<int, int> value)
    {
        var start = new int[groups + 1];
        for (var i = 0; i < n; i++) start[key(i) + 1]++;
        for (var g = 0; g < groups; g++) start[g + 1] += start[g];
        var fill = (int[])start.Clone();
        var items = new int[n];
        for (var i = 0; i < n; i++) items[fill[key(i)]++] = value(i);
        return (start, items);
    }

    private Dictionary<(int, string), int> BuildChildMap()
    {
        var map = new Dictionary<(int, string), int>(Count, ChildKeyComparer.Instance);
        for (var i = 1; i < Count; i++) map.TryAdd((Parent[i], Name[i]), i);
        return map;
    }

    /// <summary>NTFS names are case-insensitive for every caller this agent has (Win32), so a
    /// lookup for "program files" must find "Program Files".</summary>
    internal sealed class ChildKeyComparer : IEqualityComparer<(int, string)>
    {
        public static readonly ChildKeyComparer Instance = new();
        public bool Equals((int, string) a, (int, string) b) =>
            a.Item1 == b.Item1 && string.Equals(a.Item2, b.Item2, StringComparison.OrdinalIgnoreCase);
        public int GetHashCode((int, string) key) =>
            HashCode.Combine(key.Item1, StringComparer.OrdinalIgnoreCase.GetHashCode(key.Item2));
    }

    // ------------------------------------------------------------------ persistence

    /// <summary>Write the tree gzip-compressed. Folders first, then files, so a reader that
    /// wants only the folders can stop half way.</summary>
    public void Save(string path)
    {
        var temp = path + ".tmp";
        using (var file = File.Create(temp))
        using (var gzip = new GZipStream(file, CompressionLevel.Fastest))
        using (var w = new BinaryWriter(gzip, Encoding.UTF8))
        {
            w.Write(Magic);
            w.Write(FormatVersion);
            w.Write(Volume);
            w.Write(ScannedAt.ToUnixTimeSeconds());
            w.Write(TotalBytes);
            w.Write(FreeBytes);
            w.Write(Count);
            for (var i = 0; i < Count; i++)
            {
                w.Write(Parent[i]);
                w.Write(Name[i]);
                w.Write(Size[i]);
                w.Write(Allocated[i]);
                w.Write(Files[i]);
                w.Write(SubDirs[i]);
            }
            w.Write(FileCount);
            for (var i = 0; i < FileCount; i++)
            {
                w.Write(FileDir[i]);
                w.Write(FileName[i]);
                w.Write(FileSize[i]);
                w.Write(FileAllocated[i]);
            }
        }
        // Replace in one step. A crash halfway through writing must leave the PREVIOUS
        // scan readable, not a truncated file that fails to load until tomorrow.
        File.Move(temp, path, overwrite: true);
    }

    /// <summary>Read a tree written by <see cref="Save"/>, or null when the file is missing,
    /// truncated, or from a format this build does not know. A missing tree only means "no
    /// sizes yet", and it must never cost a directory listing.
    ///
    /// <paramref name="includeFiles"/> false stops after the folders. That is what the
    /// directory listing uses: folder totals already include every file under them.</summary>
    public static FolderTree? Load(string path, bool includeFiles = true)
    {
        try
        {
            if (!File.Exists(path)) return null;
            using var file = File.OpenRead(path);
            using var gzip = new GZipStream(file, CompressionMode.Decompress);
            using var r = new BinaryReader(gzip, Encoding.UTF8);
            if (r.ReadUInt32() != Magic || r.ReadInt32() != FormatVersion) return null;
            var volume = r.ReadString();
            var scannedAt = DateTimeOffset.FromUnixTimeSeconds(r.ReadInt64());
            var total = r.ReadInt64();
            var free = r.ReadInt64();
            var count = r.ReadInt32();
            if (count < 1 || count > 50_000_000) return null;

            var parent = new int[count];
            var name = new string[count];
            var size = new long[count];
            var alloc = new long[count];
            var files = new long[count];
            var dirs = new int[count];
            for (var i = 0; i < count; i++)
            {
                parent[i] = r.ReadInt32();
                name[i] = r.ReadString();
                size[i] = r.ReadInt64();
                alloc[i] = r.ReadInt64();
                files[i] = r.ReadInt64();
                dirs[i] = r.ReadInt32();
                // The ordering invariant Depth() relies on, checked rather than trusted.
                if (i > 0 && (parent[i] < 0 || parent[i] >= i)) return null;
            }

            int[] fileDir = [];
            string[] fileName = [];
            long[] fileSize = [], fileAlloc = [];
            if (includeFiles)
            {
                var fileCount = r.ReadInt32();
                if (fileCount < 0 || fileCount > 200_000_000) return null;
                fileDir = new int[fileCount];
                fileName = new string[fileCount];
                fileSize = new long[fileCount];
                fileAlloc = new long[fileCount];
                for (var i = 0; i < fileCount; i++)
                {
                    fileDir[i] = r.ReadInt32();
                    fileName[i] = r.ReadString();
                    fileSize[i] = r.ReadInt64();
                    fileAlloc[i] = r.ReadInt64();
                    if (fileDir[i] < 0 || fileDir[i] >= count) return null;
                }
            }
            return new FolderTree
            {
                Volume = volume, ScannedAt = scannedAt, TotalBytes = total, FreeBytes = free,
                Parent = parent, Name = name, Size = size, Allocated = alloc, Files = files,
                SubDirs = dirs, FileDir = fileDir, FileName = fileName, FileSize = fileSize,
                FileAllocated = fileAlloc, HasFiles = includeFiles,
            };
        }
        catch (Exception e) when (e is IOException or InvalidDataException
                                    or EndOfStreamException or UnauthorizedAccessException)
        {
            return null;
        }
    }
}
