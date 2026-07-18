using System.Diagnostics;
using System.Runtime.InteropServices;

namespace TempMonitorAgent.Fleet.Shell;

/// <summary>
/// Windows process-tree helpers for the persistent shells. Two jobs, both best-effort
/// (a failing P/Invoke logs nothing and never throws into the caller):
///
///  * <see cref="KillDescendants"/> ends the processes a shell spawned WITHOUT touching the
///    shell itself -- that's Ctrl-C / a per-submission timeout, where the session must
///    survive with its cwd and variables intact. (Process.Kill(entireProcessTree:true) is
///    no use here: it would take the shell down too.)
///
///  * <see cref="KillOnCloseJob"/> is a job object with KILL_ON_JOB_CLOSE. Shells are
///    assigned to it, so when the agent process goes away -- including the hard
///    Environment.Exit(17) a self-update uses, which skips every finalizer -- the OS tears
///    the shells down with it. Without this a SYSTEM shell could be orphaned across a
///    service restart.
/// </summary>
public static class ProcessTree
{
    /// <summary>Kill every descendant of <paramref name="rootPid"/> (children, grandchildren,
    /// …) but not the root. Uses a single process snapshot to build the parent map.</summary>
    public static void KillDescendants(int rootPid)
    {
        List<int> descendants;
        try { descendants = Descendants(rootPid); }
        catch { return; }

        // Kill children before parents so a parent can't respawn a just-killed child. The
        // snapshot ordering isn't guaranteed, so just kill each pid we found individually.
        foreach (var pid in descendants)
        {
            try
            {
                using var p = Process.GetProcessById(pid);
                p.Kill(entireProcessTree: true);
            }
            catch { /* already gone, or access denied on a system pid we don't own */ }
        }
    }

    private static List<int> Descendants(int rootPid)
    {
        var childrenByParent = new Dictionary<int, List<int>>();
        var nameByPid = new Dictionary<int, string>();
        var snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if (snapshot == IntPtr.Zero || snapshot == INVALID_HANDLE_VALUE)
            return new List<int>();
        try
        {
            var entry = new PROCESSENTRY32 { dwSize = (uint)Marshal.SizeOf<PROCESSENTRY32>() };
            if (Process32First(snapshot, ref entry))
            {
                do
                {
                    var pid = (int)entry.th32ProcessID;
                    var ppid = (int)entry.th32ParentProcessID;
                    nameByPid[pid] = entry.szExeFile ?? "";
                    if (!childrenByParent.TryGetValue(ppid, out var list))
                        childrenByParent[ppid] = list = new List<int>();
                    list.Add(pid);
                } while (Process32Next(snapshot, ref entry));
            }
        }
        finally { CloseHandle(snapshot); }

        // BFS down from the root. The parent map can contain stale/PID-reused edges, so cap
        // the walk defensively.
        var result = new List<int>();
        var queue = new Queue<int>();
        queue.Enqueue(rootPid);
        var guard = 0;
        while (queue.Count > 0 && guard++ < 100_000)
        {
            var current = queue.Dequeue();
            if (!childrenByParent.TryGetValue(current, out var kids)) continue;
            foreach (var kid in kids)
            {
                if (kid == current || kid == rootPid) continue; // no self/root cycles
                // Skip the console host: it's the shell's own infrastructure, not a workload
                // the submission spawned, and killing it destabilizes the shell's I/O so the
                // NEXT submission fails. (conhost has no children, so skipping it is complete.)
                var name = nameByPid.TryGetValue(kid, out var n) ? n : "";
                if (string.Equals(name, "conhost.exe", StringComparison.OrdinalIgnoreCase)) continue;
                result.Add(kid);
                queue.Enqueue(kid);
            }
        }
        return result;
    }

    // ---------------- Kill-on-close job ----------------

    private static readonly object _jobGate = new();
    private static IntPtr _job = IntPtr.Zero;

    /// <summary>Assign a process to the shared kill-on-close job, creating the job on first
    /// use. Best-effort: on any failure the process simply isn't job-managed (it will still
    /// be disposed normally on a graceful shutdown; only a hard Exit could orphan it).</summary>
    public static void AssignToKillOnCloseJob(IntPtr processHandle)
    {
        try
        {
            lock (_jobGate)
            {
                if (_job == IntPtr.Zero)
                    _job = CreateKillOnCloseJob();
                if (_job != IntPtr.Zero)
                    AssignProcessToJobObject(_job, processHandle);
            }
        }
        catch { /* best-effort */ }
    }

    private static IntPtr CreateKillOnCloseJob()
    {
        var job = CreateJobObject(IntPtr.Zero, null);
        if (job == IntPtr.Zero) return IntPtr.Zero;

        var info = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        {
            BasicLimitInformation = new JOBOBJECT_BASIC_LIMIT_INFORMATION
            {
                LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
            },
        };
        var length = Marshal.SizeOf<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>();
        var ptr = Marshal.AllocHGlobal(length);
        try
        {
            Marshal.StructureToPtr(info, ptr, false);
            if (!SetInformationJobObject(job, JobObjectExtendedLimitInformation, ptr, (uint)length))
            {
                CloseHandle(job);
                return IntPtr.Zero;
            }
        }
        finally { Marshal.FreeHGlobal(ptr); }
        return job;
    }

    // ---------------- P/Invoke ----------------

    private const uint TH32CS_SNAPPROCESS = 0x00000002;
    private static readonly IntPtr INVALID_HANDLE_VALUE = new(-1);
    private const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000;
    private const int JobObjectExtendedLimitInformation = 9;

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Ansi)]
    private struct PROCESSENTRY32
    {
        public uint dwSize;
        public uint cntUsage;
        public uint th32ProcessID;
        public IntPtr th32DefaultHeapID;
        public uint th32ModuleID;
        public uint cntThreads;
        public uint th32ParentProcessID;
        public int pcPriClassBase;
        public uint dwFlags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)]
        public string szExeFile;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_BASIC_LIMIT_INFORMATION
    {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IO_COUNTERS
    {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr CreateToolhelp32Snapshot(uint dwFlags, uint th32ProcessID);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool Process32First(IntPtr hSnapshot, ref PROCESSENTRY32 lppe);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool Process32Next(IntPtr hSnapshot, ref PROCESSENTRY32 lppe);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr hObject);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateJobObject(IntPtr lpJobAttributes, string? lpName);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(
        IntPtr hJob, int infoClass, IntPtr lpInfo, uint cbInfoLength);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(IntPtr hJob, IntPtr hProcess);
}
