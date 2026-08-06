using System.Diagnostics;
using System.Management;
using System.Runtime.InteropServices;
using System.Text;

namespace TempMonitorAgent.Telemetry;

/// <summary>One process as this machine sees it, in the shape the hub stores.</summary>
public sealed class ProcessEntry
{
    public int Pid { get; init; }
    public string Name { get; init; } = "";
    /// <summary>Share of the WHOLE machine's CPU, the way Task Manager's Processes tab
    /// counts it -- a process saturating one core of eight reads 12.5%, not 100%.</summary>
    public double CpuPercent { get; set; }
    public double MemoryMb { get; init; }
    public string User { get; init; } = "";
    public int Session { get; init; }
    public string Path { get; init; } = "";
    public long? StartedAt { get; init; }
    public IReadOnlyList<string> Services { get; set; } = Array.Empty<string>();
}

/// <summary>What one sample produced: the entries, plus the facts that make the numbers
/// interpretable (how many cores the percentages are shares of, how wide the window was).</summary>
public sealed class ProcessSnapshot
{
    public IReadOnlyList<ProcessEntry> Processes { get; init; } = Array.Empty<ProcessEntry>();
    public int CpuCores { get; init; }
    public double MemoryTotalMb { get; init; }
    public int SampleMillis { get; init; }
    public int Truncated { get; init; }
}

/// <summary>
/// Reads this machine's running processes, with CPU measured across two samples.
///
/// **CPU is a RATE, so it cannot come from one look.** A process's TotalProcessorTime is
/// cumulative since it started; the useful number is how much of it accrued between two
/// moments, divided by how much CPU time the machine had to give in that window
/// (elapsed × logical processors). That is why <see cref="Sample"/> returns nothing on its
/// first call: it has a baseline and no window yet. A reader that instead reported
/// TotalProcessorTime/uptime -- the one-look answer -- would show a browser that has been
/// open since breakfast at a permanent 4% while it pins a core right now.
///
/// **Everything here is best-effort per process.** Reading another process is a privileged
/// operation that fails for perfectly ordinary reasons: PPL-protected antivirus, the
/// kernel's own pseudo-processes, and anything that exits between the enumeration and the
/// read. Every accessor is wrapped, and a process that will not answer is reported with
/// whatever it did answer rather than dropped -- an operator looking for the thing eating
/// their CPU is not helped by its absence.
///
/// **Metadata is cached per pid, keyed on the name.** An image path, a session and an owner
/// cannot change for a live process, so re-reading them every five seconds would be pure
/// syscall cost. The name is part of the cache key because Windows recycles pids: a stale
/// entry surviving onto a new process would put the wrong path in a tooltip -- and, worse,
/// the wrong image behind a restart.
/// </summary>
public static class ProcessReader
{
    /// <summary>Cap on what one report carries. A workstation runs 250-400 processes and a
    /// terminal server can run thousands; the busiest ones are what somebody opened the card
    /// to find, so the list is sorted before it is cut and the remainder is COUNTED rather
    /// than silently dropped. Mirrors hub processes.MAX_PROCESSES.</summary>
    public const int MaxProcesses = 400;

    private static readonly Lock Gate = new();

    // pid -> the cumulative CPU time it had at the previous sample, and when that was.
    private static Dictionary<int, long>? _previousCpuTicks;
    private static DateTimeOffset _previousAt;

    // pid -> the facts that cannot change while it lives. Keyed with the process name so a
    // recycled pid cannot inherit another program's path, session or owner.
    private static readonly Dictionary<int, CachedFacts> _facts = new();

    private readonly record struct CachedFacts(
        string Name, string Path, int Session, string User, long? StartedAt);

    // account SID -> DOMAIN\user. Most processes on a machine share a handful of identities,
    // and LookupAccountSid is the one genuinely slow call in this file.
    private static readonly Dictionary<string, string> _accountBySid = new();

    // Which services live in which process. A single WMI query, and re-run at most this
    // often: services move between processes only when one is restarted, which is rare
    // enough that paying for the query on every five-second sample would be waste.
    private static readonly TimeSpan ServiceMapMaxAge = TimeSpan.FromSeconds(30);
    private static Dictionary<int, List<string>> _servicesByPid = new();
    private static DateTimeOffset _servicesReadAt = DateTimeOffset.MinValue;

    /// <summary>Forget the CPU baseline and the caches. Called when the hub stops asking for
    /// process reports, so that a card reopened an hour later measures a fresh window rather
    /// than averaging the CPU over the whole gap.</summary>
    public static void Reset()
    {
        lock (Gate)
        {
            _previousCpuTicks = null;
            _previousAt = default;
            _facts.Clear();
        }
    }

    /// <summary>
    /// Take one sample. Returns null on the first call after a <see cref="Reset"/> -- that
    /// call establishes the CPU baseline, and there is no honest percentage to report
    /// against a window of zero width.
    /// </summary>
    public static ProcessSnapshot? Sample()
    {
        var now = DateTimeOffset.UtcNow;
        var raw = Enumerate();

        Dictionary<int, long>? previous;
        DateTimeOffset previousAt;
        lock (Gate)
        {
            previous = _previousCpuTicks;
            previousAt = _previousAt;
            _previousCpuTicks = raw.CpuTicks;
            _previousAt = now;
        }

        if (previous is null) return null;

        var windowSeconds = (now - previousAt).TotalSeconds;
        if (windowSeconds <= 0) return null;

        // The denominator: how much CPU time the machine had to hand out over the window.
        // ProcessorCount rather than a single core, which is what makes these percentages
        // sum to something near the machine's own load rather than to 800% on an 8-core box.
        var capacity = windowSeconds * Math.Max(1, Environment.ProcessorCount);
        foreach (var entry in raw.Entries)
        {
            if (!previous.TryGetValue(entry.Pid, out var before)) continue;   // new since the last sample
            if (!raw.CpuTicks.TryGetValue(entry.Pid, out var after)) continue;
            var deltaSeconds = TimeSpan.FromTicks(Math.Max(0, after - before)).TotalSeconds;
            // Clamped: a pid recycled inside the window can produce a delta that is not this
            // process's work at all, and 4000% in a usage column reads as a bug in the console.
            entry.CpuPercent = Math.Clamp(deltaSeconds / capacity * 100.0, 0, 100);
        }

        var services = ReadServiceMap();
        foreach (var entry in raw.Entries)
        {
            if (services.TryGetValue(entry.Pid, out var names)) entry.Services = names;
        }

        // Busiest first, so what survives the cap is what the operator came looking for.
        // Memory breaks the tie: on an idle machine every CPU figure is zero, and an
        // arbitrary order there would reshuffle the whole table between refreshes.
        var ordered = raw.Entries
            .OrderByDescending(e => e.CpuPercent)
            .ThenByDescending(e => e.MemoryMb)
            .ToList();
        var truncated = Math.Max(0, ordered.Count - MaxProcesses);
        if (truncated > 0) ordered = ordered.Take(MaxProcesses).ToList();

        return new ProcessSnapshot
        {
            Processes = ordered,
            CpuCores = Environment.ProcessorCount,
            MemoryTotalMb = TotalPhysicalMemoryMb(),
            SampleMillis = (int)Math.Round(windowSeconds * 1000),
            Truncated = truncated,
        };
    }

    private readonly record struct RawSample(
        List<ProcessEntry> Entries, Dictionary<int, long> CpuTicks);

    private static RawSample Enumerate()
    {
        var entries = new List<ProcessEntry>();
        var cpuTicks = new Dictionary<int, long>();
        var live = new HashSet<int>();

        Process[] all;
        try { all = Process.GetProcesses(); }
        catch { return new RawSample(entries, cpuTicks); }

        foreach (var process in all)
        {
            using (process)
            {
                int pid;
                string name;
                try
                {
                    pid = process.Id;
                    name = process.ProcessName ?? "";
                }
                catch { continue; }                       // exited between listing and reading
                if (string.IsNullOrEmpty(name)) continue;
                live.Add(pid);

                long ticks = 0;
                // Fails for the Idle/System pseudo-processes and for anything protected. Not
                // worth dropping the row over -- the memory figure and the name still answer
                // "what is this", and the CPU column reads as unknown.
                try { ticks = process.TotalProcessorTime.Ticks; } catch { }
                cpuTicks[pid] = ticks;

                double memoryMb = 0;
                try { memoryMb = process.WorkingSet64 / 1024.0 / 1024.0; } catch { }

                var facts = FactsFor(pid, name);
                entries.Add(new ProcessEntry
                {
                    Pid = pid,
                    Name = name,
                    MemoryMb = Math.Round(memoryMb, 1),
                    User = facts.User,
                    Session = facts.Session,
                    Path = facts.Path,
                    StartedAt = facts.StartedAt,
                });
            }
        }

        // Drop cache entries for processes that have gone. Without this the dictionary grows
        // for the life of the service on a machine that churns processes (a build agent, a
        // login script), and every stale entry is a pid waiting to be reused.
        lock (Gate)
        {
            foreach (var pid in _facts.Keys.Where(p => !live.Contains(p)).ToList())
                _facts.Remove(pid);
        }

        return new RawSample(entries, cpuTicks);
    }

    private static CachedFacts FactsFor(int pid, string name)
    {
        lock (Gate)
        {
            if (_facts.TryGetValue(pid, out var cached) &&
                string.Equals(cached.Name, name, StringComparison.OrdinalIgnoreCase))
                return cached;
        }

        var facts = ReadFacts(pid, name);
        lock (Gate) { _facts[pid] = facts; }
        return facts;
    }

    private static CachedFacts ReadFacts(int pid, string name)
    {
        string path = "";
        string user = "";
        int session = 0;
        long? startedAt = null;

        if (ProcessIdToSessionId((uint)pid, out var sessionId)) session = (int)sessionId;

        // QUERY_LIMITED_INFORMATION is the least we can ask for and still read an image path
        // and a token -- deliberately not QUERY_INFORMATION, which is refused for protected
        // processes that would happily answer the limited form.
        var handle = OpenProcess(ProcessQueryLimitedInformation, false, (uint)pid);
        if (handle != IntPtr.Zero)
        {
            try
            {
                path = ImagePath(handle);
                user = OwnerOf(handle);
                startedAt = StartedAt(handle);
            }
            finally { CloseHandle(handle); }
        }

        return new CachedFacts(name, path, session, user, startedAt);
    }

    private static string ImagePath(IntPtr handle)
    {
        var buffer = new StringBuilder(1024);
        int size = buffer.Capacity;
        // Win32 path form (0), not the native \Device\HarddiskVolume2\... form: this string
        // is shown to an operator and handed to CreateProcess by the restart executor.
        return QueryFullProcessImageNameW(handle, 0, buffer, ref size)
            ? buffer.ToString(0, size)
            : "";
    }

    private static long? StartedAt(IntPtr handle)
    {
        if (!GetProcessTimes(handle, out long creation, out _, out _, out _)) return null;
        try { return DateTimeOffset.FromFileTime(creation).ToUnixTimeSeconds(); }
        catch { return null; }
    }

    /// <summary>DOMAIN\user for the process's token, or "" if it will not say.
    ///
    /// Via the token rather than WMI's Win32_Process.GetOwner, which is one out-of-process
    /// COM call PER PROCESS -- several hundred of them, on a five-second cadence, is enough
    /// to be the most expensive thing this agent does.</summary>
    private static string OwnerOf(IntPtr processHandle)
    {
        if (!OpenProcessToken(processHandle, TokenQuery, out var token)) return "";
        try
        {
            GetTokenInformation(token, TokenUserClass, IntPtr.Zero, 0, out int needed);
            if (needed <= 0) return "";
            var buffer = Marshal.AllocHGlobal(needed);
            try
            {
                if (!GetTokenInformation(token, TokenUserClass, buffer, needed, out _)) return "";
                var tokenUser = Marshal.PtrToStructure<TOKEN_USER>(buffer);
                if (!ConvertSidToStringSidW(tokenUser.User.Sid, out var sidPtr)) return "";
                string sid;
                try { sid = Marshal.PtrToStringUni(sidPtr) ?? ""; }
                finally { LocalFree(sidPtr); }
                if (sid.Length == 0) return "";

                lock (Gate)
                {
                    if (_accountBySid.TryGetValue(sid, out var known)) return known;
                }
                var resolved = AccountFromSid(tokenUser.User.Sid);
                lock (Gate) { _accountBySid[sid] = resolved; }
                return resolved;
            }
            finally { Marshal.FreeHGlobal(buffer); }
        }
        finally { CloseHandle(token); }
    }

    private static string AccountFromSid(IntPtr sid)
    {
        int nameLength = 0, domainLength = 0;
        LookupAccountSidW(null, sid, null, ref nameLength, null, ref domainLength, out _);
        if (nameLength == 0) return "";
        var accountName = new StringBuilder(nameLength);
        var domainName = new StringBuilder(Math.Max(1, domainLength));
        if (!LookupAccountSidW(null, sid, accountName, ref nameLength,
                               domainName, ref domainLength, out _))
            return "";
        var domain = domainName.ToString();
        var account = accountName.ToString();
        return string.IsNullOrEmpty(domain) ? account : $"{domain}\\{account}";
    }

    /// <summary>pid -> the services running inside it. One WMI query for the whole machine,
    /// re-read at most every <see cref="ServiceMapMaxAge"/> -- see the field's note.</summary>
    public static Dictionary<int, List<string>> ReadServiceMap(bool force = false)
    {
        lock (Gate)
        {
            if (!force && DateTimeOffset.UtcNow - _servicesReadAt < ServiceMapMaxAge)
                return _servicesByPid;
        }

        var map = new Dictionary<int, List<string>>();
        try
        {
            // ProcessId <> 0 filters out everything that is not currently running, which on a
            // typical machine is more than half the service list.
            using var searcher = new ManagementObjectSearcher(
                "SELECT Name, ProcessId FROM Win32_Service WHERE ProcessId <> 0");
            foreach (ManagementObject service in searcher.Get())
            {
                using (service)
                {
                    int pid;
                    try { pid = Convert.ToInt32(service["ProcessId"]); }
                    catch { continue; }
                    var name = service["Name"] as string;
                    if (pid <= 0 || string.IsNullOrEmpty(name)) continue;
                    if (!map.TryGetValue(pid, out var list)) map[pid] = list = new List<string>();
                    list.Add(name);
                }
            }
        }
        catch
        {
            // WMI being unavailable costs the service labels and nothing else: the process
            // list is still complete, and the restart executor reports what it could not
            // determine rather than guessing.
            lock (Gate) { _servicesReadAt = DateTimeOffset.UtcNow; }
            return _servicesByPid;
        }

        foreach (var list in map.Values) list.Sort(StringComparer.OrdinalIgnoreCase);
        lock (Gate)
        {
            _servicesByPid = map;
            _servicesReadAt = DateTimeOffset.UtcNow;
            return map;
        }
    }

    /// <summary>The image path of one live process, or "" if it will not say.
    ///
    /// Its own entry point rather than a lookup in the last sample, deliberately: the
    /// restart executor is about to LAUNCH this path, and reading it out of a cache that was
    /// filled seconds ago is exactly the window in which a pid gets recycled. It also must
    /// not go through <see cref="Sample"/>, which would move the CPU baseline the reporter
    /// is measuring against.</summary>
    public static string ImagePathForPid(int pid)
    {
        var handle = OpenProcess(ProcessQueryLimitedInformation, false, (uint)pid);
        if (handle == IntPtr.Zero) return "";
        try { return ImagePath(handle); }
        finally { CloseHandle(handle); }
    }

    /// <summary>Services hosted by one pid, read fresh. Used by the restart executor, which
    /// must not act on a cached map -- the whole point of the question there is what is in
    /// that process RIGHT NOW.</summary>
    public static IReadOnlyList<string> ServicesForPid(int pid) =>
        ReadServiceMap(force: true).TryGetValue(pid, out var names)
            ? names
            : Array.Empty<string>();

    private static double TotalPhysicalMemoryMb()
    {
        var status = new MEMORYSTATUSEX { dwLength = (uint)Marshal.SizeOf<MEMORYSTATUSEX>() };
        if (!GlobalMemoryStatusEx(ref status)) return 0;
        return Math.Round(status.ullTotalPhys / 1024.0 / 1024.0, 0);
    }

    // ---------------- P/Invoke ----------------

    private const uint ProcessQueryLimitedInformation = 0x1000;
    private const uint TokenQuery = 0x0008;
    private const int TokenUserClass = 1;   // TOKEN_INFORMATION_CLASS.TokenUser

    [StructLayout(LayoutKind.Sequential)]
    private struct SID_AND_ATTRIBUTES
    {
        public IntPtr Sid;
        public uint Attributes;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct TOKEN_USER
    {
        public SID_AND_ATTRIBUTES User;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct MEMORYSTATUSEX
    {
        public uint dwLength;
        public uint dwMemoryLoad;
        public ulong ullTotalPhys;
        public ulong ullAvailPhys;
        public ulong ullTotalPageFile;
        public ulong ullAvailPageFile;
        public ulong ullTotalVirtual;
        public ulong ullAvailVirtual;
        public ulong ullAvailExtendedVirtual;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr OpenProcess(uint access, bool inheritHandle, uint pid);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool ProcessIdToSessionId(uint pid, out uint sessionId);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool QueryFullProcessImageNameW(
        IntPtr process, uint flags, StringBuilder exeName, ref int size);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetProcessTimes(
        IntPtr process, out long creation, out long exit, out long kernel, out long user);

    [DllImport("kernel32.dll")]
    private static extern bool GlobalMemoryStatusEx(ref MEMORYSTATUSEX buffer);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool GetTokenInformation(
        IntPtr token, int infoClass, IntPtr info, int length, out int returnLength);

    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool ConvertSidToStringSidW(IntPtr sid, out IntPtr stringSid);

    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool LookupAccountSidW(
        string? systemName, IntPtr sid, StringBuilder? name, ref int nameLength,
        StringBuilder? domain, ref int domainLength, out int use);

    [DllImport("kernel32.dll")]
    private static extern IntPtr LocalFree(IntPtr handle);
}
