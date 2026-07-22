using System.Runtime.InteropServices;

namespace TempMonitorAgent.Backup;

/// <summary>
/// Temporarily mounts a logged-off user's NTUSER.DAT so their shell-folder registry can be
/// read. Roadmap #1b.
///
/// Why this exists: %Desktop% and friends resolve through each user's OWN registry, which
/// is the only way to follow OneDrive Known Folder Move. That hive is available under HKU
/// only while the user is signed in — and on a shared PC most profiles are not. Without
/// this, every logged-off user's redirected folders would be unresolvable and their data
/// would go unbacked-up, which is precisely the silent failure the token grammar exists to
/// avoid.
///
/// Requires SeRestorePrivilege and SeBackupPrivilege, which the agent has as SYSTEM.
/// Everything here fails soft: an unmountable hive means that user contributes no known
/// folders, and the hub reports it as a problem an operator can see.
///
/// ALWAYS unload what you load. A leaked mount keeps the profile's hive file open, which
/// blocks the real user from signing in cleanly — so <see cref="TryUnload"/> is called in a
/// finally, and it retries briefly because the unload can race the reads that just
/// finished releasing their handles.
/// </summary>
internal static class HiveMount
{
    private const int ErrorSuccess = 0;

    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern int RegLoadKeyW(IntPtr hKey, string lpSubKey, string lpFile);

    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern int RegUnLoadKeyW(IntPtr hKey, string lpSubKey);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool OpenProcessToken(IntPtr processHandle, uint desiredAccess,
                                                out IntPtr tokenHandle);

    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool LookupPrivilegeValueW(string? systemName, string name,
                                                     out long luid);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool AdjustTokenPrivileges(IntPtr tokenHandle, bool disableAll,
                                                     ref TokenPrivileges newState,
                                                     int bufferLength, IntPtr previousState,
                                                     IntPtr returnLength);

    [DllImport("kernel32.dll")]
    private static extern IntPtr GetCurrentProcess();

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);

    [StructLayout(LayoutKind.Sequential)]
    private struct TokenPrivileges
    {
        public int PrivilegeCount;
        public long Luid;
        public int Attributes;
    }

    private static readonly IntPtr HkeyUsers = new(unchecked((int)0x80000003));
    private const uint TokenAdjustPrivileges = 0x0020;
    private const uint TokenQuery = 0x0008;
    private const int SePrivilegeEnabled = 0x00000002;

    private static bool _privilegesEnabled;
    private static readonly Lock Gate = new();

    /// <summary>Mount <paramref name="hivePath"/> at HKU\<paramref name="mountName"/>.
    /// Returns false rather than throwing — the caller degrades to "no known folders".</summary>
    public static bool TryLoad(string mountName, string hivePath)
    {
        lock (Gate)
        {
            if (!EnsurePrivileges()) return false;
            return RegLoadKeyW(HkeyUsers, mountName, hivePath) == ErrorSuccess;
        }
    }

    /// <summary>Unmount, retrying briefly. A handle opened by the read above can take a
    /// moment to be released after Dispose, and a failed unload leaves the profile's hive
    /// locked — worse than the read we just did was worth.</summary>
    public static bool TryUnload(string mountName)
    {
        for (int attempt = 0; attempt < 5; attempt++)
        {
            lock (Gate)
            {
                if (RegUnLoadKeyW(HkeyUsers, mountName) == ErrorSuccess) return true;
            }
            // The handles are released by the GC finalising RegistryKey wrappers the
            // caller has already disposed; give that a chance rather than spinning.
            GC.Collect();
            GC.WaitForPendingFinalizers();
            Thread.Sleep(100);
        }
        return false;
    }

    /// <summary>Enable SeRestore/SeBackup once per process. Idempotent.</summary>
    private static bool EnsurePrivileges()
    {
        if (_privilegesEnabled) return true;
        if (!OpenProcessToken(GetCurrentProcess(),
                              TokenAdjustPrivileges | TokenQuery, out var token))
            return false;
        try
        {
            bool ok = true;
            foreach (var name in new[] { "SeRestorePrivilege", "SeBackupPrivilege" })
            {
                if (!LookupPrivilegeValueW(null, name, out var luid)) { ok = false; continue; }
                var privileges = new TokenPrivileges
                {
                    PrivilegeCount = 1,
                    Luid = luid,
                    Attributes = SePrivilegeEnabled,
                };
                if (!AdjustTokenPrivileges(token, false, ref privileges, 0, IntPtr.Zero, IntPtr.Zero))
                    ok = false;
                // AdjustTokenPrivileges reports success even when it granted nothing, so
                // the real check is the last error.
                if (Marshal.GetLastWin32Error() != ErrorSuccess) ok = false;
            }
            _privilegesEnabled = ok;
            return ok;
        }
        finally
        {
            CloseHandle(token);
        }
    }
}
