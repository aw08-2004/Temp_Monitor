namespace TempMonitorAgent.DiskUsage;

/// <summary>One folder that grew or shrank between two scans. Sizes are on-disk bytes.</summary>
internal readonly record struct FolderChange(string Path, long Before, long After)
{
    public long Delta => After - Before;
}

/// <summary>A file over the large-file threshold that was not there at the previous scan.</summary>
internal readonly record struct NewLargeFile(string Path, long Size, long Allocated);

/// <summary>
/// What changed between yesterday's tree and today's: the answer to "what is filling this
/// disk" (roadmap #27).
///
/// **Report the deepest folder that explains a change, not every ancestor of it.** A 20 GB
/// Outlook cache that grew by 5 GB also grew C:\Users, C:\Users\Ann, AppData, Local and
/// Microsoft by 5 GB each. A list of the six biggest growers would be those six, which says
/// nothing. So a folder is skipped when a single child of it moved the same way by at least
/// <see cref="ExplainedShare"/> of its own change, and the walk continues into that child.
/// A folder whose change is spread over many children (a Downloads folder that gained forty
/// files) is reported itself, and the walk stops there.
///
/// **Every pair is walked, not only the ones whose net change is big.** A folder that
/// gained 10 GB in one child and lost 10 GB in another nets to zero, and both halves are
/// worth knowing. The walk prunes only a pair where BOTH sides are under
/// <see cref="MinDelta"/>, since nothing inside such a pair can move by that much.
///
/// **On-disk bytes, not logical size.** A sparse VM disk that "grew" by 100 GB of zeros
/// fills nothing. The number that predicts a full volume is the allocation.
/// </summary>
internal static class FolderTreeDiff
{
    /// <summary>Changes smaller than this are noise: browser caches, logs, Windows Update churn.</summary>
    internal const long MinDelta = 64L * 1024 * 1024;

    /// <summary>One child "explains" its parent's change from this share upwards.</summary>
    internal const double ExplainedShare = 0.8;

    internal const int MaxChanges = 200;
    internal const int MaxNewLargeFiles = 50;

    public static List<FolderChange> Compare(FolderTree? before, FolderTree after,
                                             int max = MaxChanges, long minDelta = MinDelta)
    {
        var changes = new List<FolderChange>();
        if (before is null) return changes;

        var beforeKids = Children(before);
        var afterKids = Children(after);
        var stack = new Stack<(int Before, int After)>();
        stack.Push((0, 0));

        while (stack.Count > 0)
        {
            var (b, a) = stack.Pop();
            var sizeBefore = b >= 0 ? before.Allocated[b] : 0;
            var sizeAfter = a >= 0 ? after.Allocated[a] : 0;
            var delta = sizeAfter - sizeBefore;

            // Pair up children by name; a side that has no match is new or deleted.
            var pairs = new List<(int, int)>();
            var byName = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase);
            if (b >= 0) foreach (var c in beforeKids[b]) byName.TryAdd(before.Name[c], c);
            if (a >= 0)
            {
                foreach (var c in afterKids[a])
                {
                    if (byName.Remove(after.Name[c], out var match)) pairs.Add((match, c));
                    else pairs.Add((-1, c));
                }
            }
            foreach (var orphan in byName.Values) pairs.Add((orphan, -1));

            var explained = false;
            if (Math.Abs(delta) >= minDelta)
            {
                foreach (var (cb, ca) in pairs)
                {
                    var childDelta = (ca >= 0 ? after.Allocated[ca] : 0) - (cb >= 0 ? before.Allocated[cb] : 0);
                    if (Math.Sign(childDelta) == Math.Sign(delta)
                        && Math.Abs(childDelta) >= ExplainedShare * Math.Abs(delta))
                    {
                        explained = true;
                        break;
                    }
                }
                if (!explained)
                {
                    // Reported: the change lives here, spread over this folder's contents.
                    changes.Add(new FolderChange(
                        a >= 0 ? after.PathOf(a) : before.PathOf(b), sizeBefore, sizeAfter));
                    continue;
                }
            }

            foreach (var (cb, ca) in pairs)
            {
                var hb = cb >= 0 ? before.Allocated[cb] : 0;
                var ha = ca >= 0 ? after.Allocated[ca] : 0;
                if (hb >= minDelta || ha >= minDelta) stack.Push((cb, ca));
            }
        }

        changes.Sort((x, y) => Math.Abs(y.Delta).CompareTo(Math.Abs(x.Delta)));
        if (changes.Count > max) changes.RemoveRange(max, changes.Count - max);
        return changes;
    }

    /// <summary>The files over the large-file threshold in <paramref name="after"/> whose path
    /// was not in <paramref name="before"/>, biggest first. Both trees need their files.</summary>
    public static List<NewLargeFile> NewLargeFiles(FolderTree? before, FolderTree after,
                                                   int max = MaxNewLargeFiles)
    {
        var result = new List<NewLargeFile>();
        if (before is null || !before.HasFiles || !after.HasFiles) return result;
        var known = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        for (var i = 0; i < before.FileCount; i++)
            if (before.FileSize[i] >= FolderTreeBuilder.LargeFileBytes) known.Add(before.FilePathOf(i));
        for (var i = 0; i < after.FileCount; i++)
        {
            if (after.FileSize[i] < FolderTreeBuilder.LargeFileBytes) continue;
            var path = after.FilePathOf(i);
            if (!known.Contains(path))
                result.Add(new NewLargeFile(path, after.FileSize[i], after.FileAllocated[i]));
        }
        result.Sort((x, y) => y.Allocated.CompareTo(x.Allocated));
        if (result.Count > max) result.RemoveRange(max, result.Count - max);
        return result;
    }

    private static List<int>[] Children(FolderTree tree)
    {
        var kids = new List<int>[tree.Count];
        for (var i = 0; i < tree.Count; i++) kids[i] = [];
        for (var i = 1; i < tree.Count; i++) kids[tree.Parent[i]].Add(i);
        return kids;
    }
}
