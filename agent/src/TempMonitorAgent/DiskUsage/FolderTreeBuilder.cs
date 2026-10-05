namespace TempMonitorAgent.DiskUsage;

/// <summary>
/// Accumulates parsed MFT records and rolls them up into a <see cref="FolderTree"/>
/// (roadmap #27).
///
/// **One slot per MFT record, filled in whatever order the records arrive.** An extension
/// record can be read before its base, so nothing here assumes the base came first. The
/// directory flag, which only a base record carries, is taken only from one.
///
/// **Every file name is kept.** The hub keeps size history down to single files, so the tree
/// must name every one of them. On a system drive that is around a million strings, the
/// largest single cost of a scan, and the reason the scan runs once a day rather than hourly.
///
/// **What does not reach the root is left out of the tree, not hung off it.** A folder whose
/// parent chain ends at a record that is not a directory (or loops back on itself after an
/// interrupted rename) is an orphan, and chkdsk would move it to found.000. Hanging it off
/// the root would put a folder in the browser that Explorer does not show. The volume's used
/// space still comes from its cluster count, so the totals stay right either way.
/// </summary>
internal sealed class FolderTreeBuilder
{
    /// <summary>Files at least this big are listed as "new large files".</summary>
    internal const long LargeFileBytes = 100L * 1024 * 1024;

    /// <summary>NTFS keeps the root directory at a fixed record number.</summary>
    internal const uint RootRecord = 5;

    private readonly uint[] _parent;
    private readonly string?[] _name;
    private readonly sbyte[] _ns;
    private readonly byte[] _flags; // 1 = in use (seen as base), 2 = directory, 4 = has list
    private readonly long[] _size;
    private readonly long[] _alloc;

    public FolderTreeBuilder(int recordCount)
    {
        _parent = new uint[recordCount];
        _name = new string?[recordCount];
        _ns = new sbyte[recordCount];
        Array.Fill(_ns, (sbyte)-1);
        _flags = new byte[recordCount];
        _size = new long[recordCount];
        _alloc = new long[recordCount];
    }

    public void Add(MftRecord record)
    {
        var slot = record.BaseRecord;
        if (slot >= _parent.Length) return;

        if (record.RecordNumber == record.BaseRecord)
        {
            _flags[slot] |= 1;
            if (record.IsDirectory) _flags[slot] |= 2;
        }
        if (record.HasAttributeList) _flags[slot] |= 4;

        if (record.Size >= 0) _size[slot] = record.Size;
        // Added, not assigned: a file's streams can sit in different records (a compressed
        // file's WofCompressedData in an extension record), and each record reports only the
        // streams that start in it.
        _alloc[slot] += Math.Max(0, record.Allocated);

        if (record.Name is not null && MftRecordParser.Better(record.NameNamespace, _ns[slot]))
        {
            _ns[slot] = (sbyte)record.NameNamespace;
            _parent[slot] = record.ParentRecord;
            _name[slot] = record.Name;
        }
    }

    public FolderTree Build(string volume, DateTimeOffset scannedAt, long totalBytes, long freeBytes)
    {
        var n = _parent.Length;
        bool IsDir(long r) => r >= 0 && r < n && (_flags[r] & 3) == 3;

        // Depth of every directory, resolving the parent chain iteratively. 0 = unresolved,
        // -1 = orphan or cycle, otherwise depth + 1 (so the root is 1).
        var depth = new int[n];
        if (IsDir(RootRecord)) depth[RootRecord] = 1;
        var chain = new List<uint>();
        for (uint r = 0; r < n; r++)
        {
            if (!IsDir(r) || depth[r] != 0) continue;
            chain.Clear();
            var cur = r;
            var result = -1;
            while (true)
            {
                if (depth[cur] != 0) { result = depth[cur] > 0 ? depth[cur] : -1; break; }
                if (chain.Count > 4096) break;          // a loop, or nonsense
                chain.Add(cur);
                depth[cur] = -2;                        // in progress: meeting it again is a cycle
                var p = _parent[cur];
                if (!IsDir(p) || p == cur) break;
                if (depth[p] == -2) break;
                cur = p;
            }
            for (var i = chain.Count - 1; i >= 0; i--)
            {
                result = result > 0 ? result + 1 : -1;
                depth[chain[i]] = result;
            }
        }

        // Order folders by depth so every parent gets a lower index than its children. The
        // tree's readers rely on that, and it turns the rollup into one backward pass.
        var dirs = new List<uint>();
        for (uint r = 0; r < n; r++)
            if (IsDir(r) && depth[r] > 0) dirs.Add(r);
        dirs.Sort((a, b) => depth[a] != depth[b] ? depth[a].CompareTo(depth[b]) : a.CompareTo(b));

        // No root means no tree: an MFT this broken is reported as a scan with one empty
        // folder rather than as a crash, and the volume totals still go to the hub.
        if (dirs.Count == 0 || dirs[0] != RootRecord) dirs = [RootRecord];

        var index = new Dictionary<uint, int>(dirs.Count);
        for (var i = 0; i < dirs.Count; i++) index[dirs[i]] = i;

        var count = dirs.Count;
        var parent = new int[count];
        var name = new string[count];
        var size = new long[count];
        var alloc = new long[count];
        var files = new long[count];
        var subDirs = new int[count];
        for (var i = 0; i < count; i++)
        {
            var r = dirs[i];
            parent[i] = i == 0 ? 0 : index[_parent[r]];
            name[i] = i == 0 ? "" : _name[r] ?? "";
        }

        // Files are credited to the folder that holds them, and listed under it.
        var fileDir = new List<int>();
        var fileName = new List<string>();
        var fileSize = new List<long>();
        var fileAlloc = new List<long>();
        for (uint r = 0; r < n; r++)
        {
            if ((_flags[r] & 3) != 1) continue;               // in use, not a directory
            if (!index.TryGetValue(_parent[r], out var dir)) continue;
            size[dir] += _size[r];
            alloc[dir] += _alloc[r];
            files[dir]++;
            // A file with no name cannot be addressed by a path, so it counts towards its
            // folder's total and is not listed.
            if (string.IsNullOrEmpty(_name[r])) continue;
            fileDir.Add(dir);
            fileName.Add(_name[r]!);
            fileSize.Add(_size[r]);
            fileAlloc.Add(_alloc[r]);
        }

        // Children before parents: walk backwards and push each folder's totals up one level.
        for (var i = count - 1; i > 0; i--)
        {
            var p = parent[i];
            size[p] += size[i];
            alloc[p] += alloc[i];
            files[p] += files[i];
            subDirs[p] += subDirs[i] + 1;
        }

        return new FolderTree
        {
            Volume = volume, ScannedAt = scannedAt, TotalBytes = totalBytes, FreeBytes = freeBytes,
            Parent = parent, Name = name, Size = size, Allocated = alloc, Files = files,
            SubDirs = subDirs, FileDir = fileDir.ToArray(), FileName = fileName.ToArray(),
            FileSize = fileSize.ToArray(), FileAllocated = fileAlloc.ToArray(),
        };
    }
}
