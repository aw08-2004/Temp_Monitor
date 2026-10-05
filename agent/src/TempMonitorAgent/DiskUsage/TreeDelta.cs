using System.IO.Compression;
using System.Text;
using System.Text.Json;

namespace TempMonitorAgent.DiskUsage;

/// <summary>
/// Writes what changed between two scans, entry by entry, for the hub's full-depth size
/// history (roadmap #27).
///
/// **The hub keeps every folder and file, and this is how it gets them without a full tree
/// a day.** A full system drive is a million and a half paths, 15-40 MB compressed. The day
/// after, a few tens of thousands have a different size. So the agent sends only those:
/// every entry that is new, gone, or whose size, size on disk or file count moved. The hub
/// stores each as "from this day on, the value is X", which is enough to rebuild any day.
///
/// **A full tree is the same format, diffed against nothing.** The first upload, and any
/// upload after the hub says it missed one (<see cref="DiskUsageReporter"/>), is
/// <c>Write(null, tree)</c>: every entry is "new". One format, one parser on the hub, and
/// no second code path that only runs on the rare day it is needed.
///
/// **Format: gzip of one JSON object per line**, parents before their children:
/// <c>{"p":"C:\\Users","d":1,"s":123,"a":4096,"f":7}</c> for an entry that exists (d = 1 for
/// a folder, f = files under a folder), <c>{"p":"C:\\old.log","x":1}</c> for one that is gone.
/// A deleted folder lists every deleted entry under it as well. The hub must never have to
/// infer that a child went away because its parent did: inferring is how a folder deleted
/// and recreated the same day would bring back its old children.
/// </summary>
internal static class TreeDelta
{
    /// <summary>Write the change from <paramref name="before"/> (null: nothing) to
    /// <paramref name="after"/> into <paramref name="path"/>. Both trees need their files.
    /// Returns the number of lines.</summary>
    public static long Write(FolderTree? before, FolderTree after, string path)
    {
        var temp = path + ".tmp";
        long lines;
        using (var file = File.Create(temp))
        using (var gzip = new GZipStream(file, CompressionLevel.Optimal))
        using (var w = new StreamWriter(gzip, new UTF8Encoding(false), 1 << 16))
        {
            w.NewLine = "\n";
            lines = Write(before, after, w);
        }
        File.Move(temp, path, overwrite: true);
        return lines;
    }

    internal static long Write(FolderTree? before, FolderTree after, TextWriter w)
    {
        if (!after.HasFiles || (before is not null && !before.HasFiles))
            throw new InvalidOperationException("a delta needs both trees with their files");

        var aKids = after.ChildIndex();
        var aFiles = after.FileIndex();
        var bKids = before?.ChildIndex();
        var bFiles = before?.FileIndex();
        long lines = 0;

        var stack = new Stack<(int Before, int After)>();
        stack.Push((before is null ? -1 : 0, 0));
        while (stack.Count > 0)
        {
            var (b, a) = stack.Pop();
            if (a < 0)
            {
                // Gone, with everything under it.
                lines += WriteDeleted(before!, b, bKids!.Value, bFiles!.Value, w);
                continue;
            }

            var path = after.PathOf(a);
            if (b < 0 || before!.Size[b] != after.Size[a] || before.Allocated[b] != after.Allocated[a]
                || before.Files[b] != after.Files[a])
            {
                WriteEntry(w, path, true, after.Size[a], after.Allocated[a], after.Files[a]);
                lines++;
            }

            // Files of this folder.
            Dictionary<string, int>? oldFiles = null;
            if (b >= 0)
            {
                oldFiles = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase);
                var (fs, fi) = bFiles!.Value;
                for (var k = fs[b]; k < fs[b + 1]; k++) oldFiles.TryAdd(before!.FileName[fi[k]], fi[k]);
            }
            var (afs, afi) = aFiles;
            for (var k = afs[a]; k < afs[a + 1]; k++)
            {
                var f = afi[k];
                var name = after.FileName[f];
                if (oldFiles is not null && oldFiles.Remove(name, out var old)
                    && before!.FileSize[old] == after.FileSize[f]
                    && before.FileAllocated[old] == after.FileAllocated[f])
                    continue;
                WriteEntry(w, FolderTree.Join(path, name), false, after.FileSize[f],
                           after.FileAllocated[f], 0);
                lines++;
            }
            if (oldFiles is not null)
            {
                foreach (var old in oldFiles.Values)
                {
                    WriteGone(w, FolderTree.Join(path, before!.FileName[old]));
                    lines++;
                }
            }

            // Child folders, paired by name. Pushed in reverse so they pop in order, which
            // keeps the output stable for a reader comparing two uploads by eye.
            var pairs = new List<(int, int)>();
            Dictionary<string, int>? oldDirs = null;
            if (b >= 0)
            {
                oldDirs = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase);
                var (ks, ki) = bKids!.Value;
                for (var k = ks[b]; k < ks[b + 1]; k++) oldDirs.TryAdd(before!.Name[ki[k]], ki[k]);
            }
            var (aks, aki) = aKids;
            for (var k = aks[a]; k < aks[a + 1]; k++)
            {
                var c = aki[k];
                pairs.Add(oldDirs is not null && oldDirs.Remove(after.Name[c], out var match)
                    ? (match, c) : (-1, c));
            }
            if (oldDirs is not null) foreach (var gone in oldDirs.Values) pairs.Add((gone, -1));
            for (var k = pairs.Count - 1; k >= 0; k--) stack.Push(pairs[k]);
        }
        return lines;
    }

    private static long WriteDeleted(FolderTree before, int dir, (int[] Start, int[] Items) kids,
                                     (int[] Start, int[] Items) files, TextWriter w)
    {
        long lines = 0;
        var stack = new Stack<int>();
        stack.Push(dir);
        while (stack.Count > 0)
        {
            var d = stack.Pop();
            var path = before.PathOf(d);
            WriteGone(w, path);
            lines++;
            for (var k = files.Start[d]; k < files.Start[d + 1]; k++)
            {
                WriteGone(w, FolderTree.Join(path, before.FileName[files.Items[k]]));
                lines++;
            }
            for (var k = kids.Start[d]; k < kids.Start[d + 1]; k++) stack.Push(kids.Items[k]);
        }
        return lines;
    }

    private static void WriteEntry(TextWriter w, string path, bool dir, long size, long alloc, long files)
    {
        w.Write("{\"p\":");
        w.Write(JsonSerializer.Serialize(path));
        w.Write(dir ? ",\"d\":1" : ",\"d\":0");
        w.Write(",\"s\":");
        w.Write(size);
        w.Write(",\"a\":");
        w.Write(alloc);
        if (dir)
        {
            w.Write(",\"f\":");
            w.Write(files);
        }
        w.Write("}\n");
    }

    private static void WriteGone(TextWriter w, string path)
    {
        w.Write("{\"p\":");
        w.Write(JsonSerializer.Serialize(path));
        w.Write(",\"x\":1}\n");
    }
}
