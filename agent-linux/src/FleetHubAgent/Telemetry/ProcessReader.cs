namespace FleetHubAgent.Telemetry;

/// <summary>One process, in the shape the hub's <c>processes.record_snapshot</c> reads.</summary>
public sealed record ProcessEntry(
    int Pid,
    string Name,
    double CpuPercent,
    double MemoryMb,
    string User,
    string Path,
    IReadOnlyList<string> Services,
    long? StartedAt);

/// <summary>One sampled process list, with what the console needs to put it in context.</summary>
public sealed record ProcessSnapshot(
    IReadOnlyList<ProcessEntry> Processes,
    int CpuCores,
    double MemoryTotalMb,
    int SampleMillis,
    int Truncated);

/// <summary>
/// The Processes card's list on Linux (roadmap #22, "Next, in order" item 4): a walk of
/// <c>/proc</c>, demand-driven exactly like the Windows agent's reader.
///
/// <para><b>CPU needs two samples, so the first call returns null.</b> <c>/proc/&lt;pid&gt;/stat</c>
/// holds cumulative ticks since the process started; a percentage is the difference between
/// two readings over the difference in the machine's total ticks across the same window
/// (<c>/proc/stat</c>'s aggregate <c>cpu</c> line, which sums every core). That makes it a
/// share of the WHOLE machine -- 100 means every core busy -- which is what Task Manager shows
/// and what the Windows agent reports, so a Linux box and a PC read the same on one card.
/// <see cref="ProcessReporter"/> takes a baseline and samples again a second later.</para>
///
/// <para><b>A pid is not an identity; (pid, starttime) is.</b> Pids are reused, and a new
/// process that happened to land on a pid the previous sample saw would otherwise inherit the
/// old one's tick count and report a nonsense (often negative) delta.</para>
///
/// <para><b>Services come from the cgroup, not from asking systemd.</b> A process in a
/// systemd unit sits under <c>.../system.slice/&lt;unit&gt;.service</c> in
/// <c>/proc/&lt;pid&gt;/cgroup</c>, which is a file read per process. Asking systemd over D-Bus
/// would be the more "correct" route and a second IPC stack in the agent for a label.</para>
///
/// <para><b>Every read tolerates the process having exited.</b> A walk takes tens of
/// milliseconds and processes come and go inside it; a pid that vanished between the
/// directory listing and the stat read is skipped, never an error.</para>
///
/// Kernel ticks are assumed to be 100 per second (<c>USER_HZ</c>). That is the value on every
/// mainstream distribution and architecture, and it only affects <c>started_at</c> -- the CPU
/// percentage is a ratio of ticks to ticks and does not depend on it.
/// </summary>
public sealed class ProcessReader
{
    /// <summary>Matches the hub's <c>processes.MAX_PROCESSES</c>: the hub keeps the first N
    /// rows it is sent, so the agent sorts busiest-first and sends at most this many.</summary>
    public const int MaxProcesses = 400;

    private const double TicksPerSecond = 100.0;

    private readonly string _procRoot;
    private Dictionary<(int Pid, long Start), long> _previous = new();
    private long _previousTotal;
    private DateTime _previousAt;
    private Dictionary<uint, string>? _users;

    public ProcessReader(string procRoot = "/proc") => _procRoot = procRoot;

    /// <summary>Forget the baseline -- called when nobody is watching any more, so a card
    /// opened an hour later does not report an hour-long average as "current".</summary>
    public void Reset()
    {
        _previous = new();
        _previousTotal = 0;
    }

    /// <summary>Walk /proc. Null on the first call after a reset (the baseline), else a
    /// snapshot whose CPU figures cover the time since the previous call.</summary>
    public ProcessSnapshot? Sample()
    {
        var total = ParseCpuTotal(ReadOrEmpty(Path.Combine(_procRoot, "stat")));
        var bootTime = ParseBootTime(ReadOrEmpty(Path.Combine(_procRoot, "stat")));
        var memTotalKb = ParseMemTotalKb(ReadOrEmpty(Path.Combine(_procRoot, "meminfo")));
        _users ??= ParsePasswd(ReadOrEmpty("/etc/passwd"));

        var now = DateTime.UtcNow;
        var current = new Dictionary<(int, long), long>();
        var rows = new List<(ProcessEntry Entry, long Ticks, long Start)>();

        foreach (var dir in SafeDirectories(_procRoot))
        {
            if (!int.TryParse(System.IO.Path.GetFileName(dir), out var pid)) continue;
            var stat = ParseStat(ReadOrEmpty(System.IO.Path.Combine(dir, "stat")));
            if (stat is null) continue;   // exited mid-walk, or a kernel oddity
            var status = ParseStatus(ReadOrEmpty(System.IO.Path.Combine(dir, "status")));
            var ticks = stat.Value.UserTicks + stat.Value.SystemTicks;
            current[(pid, stat.Value.StartTicks)] = ticks;

            var user = status.Uid is { } uid && _users.TryGetValue(uid, out var name)
                ? name : status.Uid?.ToString() ?? "";
            long? startedAt = bootTime is null
                ? null
                : bootTime.Value + (long)(stat.Value.StartTicks / TicksPerSecond);
            rows.Add((new ProcessEntry(
                Pid: pid,
                // /proc/<pid>/status's Name is the same 15-character comm as stat's; the exe
                // basename is the name a person recognises when comm was truncated.
                Name: ExeName(dir) ?? status.Name ?? stat.Value.Comm,
                CpuPercent: 0,
                MemoryMb: status.RssKb / 1024.0,
                User: user,
                Path: ExePath(dir) ?? "",
                Services: ParseUnit(ReadOrEmpty(System.IO.Path.Combine(dir, "cgroup"))) is { } unit
                    ? [unit] : [],
                StartedAt: startedAt), ticks, stat.Value.StartTicks));
        }

        var baseline = _previous.Count == 0 || _previousTotal == 0;
        var totalDelta = total - _previousTotal;
        var previous = _previous;
        var elapsed = now - _previousAt;
        _previous = current;
        _previousTotal = total;
        _previousAt = now;
        if (baseline || totalDelta <= 0) return null;

        var entries = rows.Select(r =>
        {
            var before = previous.TryGetValue((r.Entry.Pid, r.Start), out var t) ? t : r.Ticks;
            var cpu = Math.Clamp(100.0 * (r.Ticks - before) / totalDelta, 0, 100);
            return r.Entry with { CpuPercent = cpu };
        })
        .OrderByDescending(e => e.CpuPercent)
        .ThenByDescending(e => e.MemoryMb)
        .ToList();

        var truncated = Math.Max(0, entries.Count - MaxProcesses);
        if (truncated > 0) entries.RemoveRange(MaxProcesses, truncated);
        return new ProcessSnapshot(entries, Environment.ProcessorCount, memTotalKb / 1024.0,
                                   (int)Math.Max(0, elapsed.TotalMilliseconds), truncated);
    }

    // ------------------------------------------------------------------ pure parsers

    /// <summary>The fields of <c>/proc/&lt;pid&gt;/stat</c> this reader uses, or null.
    ///
    /// <b>comm is found by the LAST close paren, not by splitting on spaces.</b> It is
    /// arbitrary text set by the process itself -- `(sd-pam)`, `(Web Content)`, or a
    /// deliberately hostile `a) R 1 2 (` -- and a naive split shifts every field after it,
    /// silently turning a start time into a tick count.</summary>
    public static (string Comm, long UserTicks, long SystemTicks, long StartTicks)? ParseStat(string text)
    {
        var open = text.IndexOf('(');
        var close = text.LastIndexOf(')');
        if (open < 0 || close <= open) return null;
        var comm = text[(open + 1)..close];
        // After ") " the fields are numbered from 3 (state). utime is 14, stime 15, starttime
        // 22 -- so indexes 11, 12 and 19 in the remainder.
        var rest = text[(close + 1)..].Split(' ', StringSplitOptions.RemoveEmptyEntries);
        if (rest.Length < 20) return null;
        if (!long.TryParse(rest[11], out var utime) || !long.TryParse(rest[12], out var stime)
            || !long.TryParse(rest[19], out var start))
            return null;
        return (comm, utime, stime, start);
    }

    /// <summary>Name, real uid and resident set size from <c>/proc/&lt;pid&gt;/status</c>.
    /// A kernel thread has no VmRSS line; that is 0 MB, which is true.</summary>
    public static (string? Name, uint? Uid, long RssKb) ParseStatus(string text)
    {
        string? name = null;
        uint? uid = null;
        long rss = 0;
        foreach (var line in text.Split('\n'))
        {
            if (line.StartsWith("Name:", StringComparison.Ordinal))
                name = line[5..].Trim();
            else if (line.StartsWith("Uid:", StringComparison.Ordinal))
            {
                var parts = line[4..].Split(['\t', ' '], StringSplitOptions.RemoveEmptyEntries);
                if (parts.Length > 0 && uint.TryParse(parts[0], out var u)) uid = u;
            }
            else if (line.StartsWith("VmRSS:", StringComparison.Ordinal))
            {
                // Tab AND space: the kernel writes "VmRSS:\t   5120 kB", and splitting on
                // spaces alone left "\t" as the first field -- every process read 0 MB, which
                // is the one wrong answer that looks plausible on a card. Caught by the test.
                var parts = line[6..].Split(['\t', ' '], StringSplitOptions.RemoveEmptyEntries);
                if (parts.Length > 0) long.TryParse(parts[0], out rss);
            }
        }
        return (name, uid, rss);
    }

    /// <summary>Sum of every column on /proc/stat's aggregate <c>cpu</c> line: the machine's
    /// total ticks across all cores, idle included.</summary>
    public static long ParseCpuTotal(string text)
    {
        foreach (var line in text.Split('\n'))
        {
            if (!line.StartsWith("cpu ", StringComparison.Ordinal)) continue;
            long sum = 0;
            foreach (var field in line[4..].Split(' ', StringSplitOptions.RemoveEmptyEntries))
                if (long.TryParse(field, out var v)) sum += v;
            return sum;
        }
        return 0;
    }

    /// <summary><c>btime</c> from /proc/stat: boot time as a Unix epoch second.</summary>
    public static long? ParseBootTime(string text)
    {
        foreach (var line in text.Split('\n'))
            if (line.StartsWith("btime ", StringComparison.Ordinal)
                && long.TryParse(line[6..].Trim(), out var v))
                return v;
        return null;
    }

    public static long ParseMemTotalKb(string text)
    {
        foreach (var line in text.Split('\n'))
        {
            if (!line.StartsWith("MemTotal:", StringComparison.Ordinal)) continue;
            var parts = line[9..].Split(['\t', ' '], StringSplitOptions.RemoveEmptyEntries);
            if (parts.Length > 0 && long.TryParse(parts[0], out var kb)) return kb;
        }
        return 0;
    }

    /// <summary>uid -> login name from /etc/passwd. Read once per reader: accounts do not
    /// change between samples, and a directory lookup per row would be an NSS call each.</summary>
    public static Dictionary<uint, string> ParsePasswd(string text)
    {
        var map = new Dictionary<uint, string>();
        foreach (var line in text.Split('\n'))
        {
            var parts = line.Split(':');
            if (parts.Length > 2 && uint.TryParse(parts[2], out var uid))
                map.TryAdd(uid, parts[0]);
        }
        return map;
    }

    /// <summary>The systemd unit a process runs in, from <c>/proc/&lt;pid&gt;/cgroup</c>, or
    /// null. Only <c>.service</c> units count -- a <c>session-3.scope</c> is a login, not a
    /// service anybody would restart, and the Windows card's Services column means the
    /// same thing.</summary>
    public static string? ParseUnit(string text)
    {
        foreach (var line in text.Split('\n'))
        {
            var path = line.Split(':', 3) is { Length: 3 } parts ? parts[2] : "";
            foreach (var segment in path.Split('/').Reverse())
                if (segment.EndsWith(".service", StringComparison.Ordinal)) return segment;
        }
        return null;
    }

    // ------------------------------------------------------------------ I/O helpers

    private static string? ExePath(string dir)
    {
        try { return new FileInfo(System.IO.Path.Combine(dir, "exe")).LinkTarget; }
        catch { return null; }   // kernel threads and other users' processes without CAP_SYS_PTRACE
    }

    private static string? ExeName(string dir)
    {
        var path = ExePath(dir);
        if (string.IsNullOrEmpty(path)) return null;
        // A binary replaced on disk by an upgrade reads back as "/usr/bin/foo (deleted)".
        const string deleted = " (deleted)";
        if (path.EndsWith(deleted, StringComparison.Ordinal)) path = path[..^deleted.Length];
        return System.IO.Path.GetFileName(path);
    }

    private static IEnumerable<string> SafeDirectories(string root)
    {
        try { return Directory.EnumerateDirectories(root).ToList(); }
        catch { return []; }
    }

    private static string ReadOrEmpty(string path)
    {
        try { return File.ReadAllText(path); }
        catch { return ""; }
    }
}
