using System.Runtime.InteropServices;
using System.Text;

namespace TempMonitorAgent.Remote;

/// <summary>
/// Launches a process as SYSTEM inside the active interactive console session (roadmap #2).
///
/// Why this is necessary and why it is shaped this way:
///   * The agent runs as a Windows Service in <b>session 0</b>. Session 0 Isolation (since
///     Vista) means that session has no rendered desktop at all, so screen-capture APIs there
///     read nothing. The capture/input helper MUST run in the interactive session.
///   * We run the helper as <b>SYSTEM-in-session</b> rather than as the logged-in user:
///     duplicate the service's own SYSTEM token, retarget its session id to the console
///     session, and CreateProcessAsUser into that session. Running as SYSTEM (not a user
///     token) is what later lets the helper OpenDesktop("Winlogon") for the secure desktop
///     (UAC prompts, Ctrl+Alt+Del), which is walled off from any user-token process by design.
///
/// Retargeting a token's session id needs SE_TCB_NAME; CreateProcessAsUser needs
/// SeAssignPrimaryToken + SeIncreaseQuota. SYSTEM holds all three; SeTcb is enabled here
/// explicitly because it is often present-but-disabled.
///
/// Everything fails soft with a described error rather than throwing -- the caller (the
/// command executor) turns that into a command result the operator can read.
/// </summary>
internal static class SessionInjector
{
    public readonly record struct InjectionResult(bool Ok, uint Pid, uint SessionId, string? Error)
    {
        public static InjectionResult Fail(string error) => new(false, 0, 0, error);
        public static InjectionResult Success(uint pid, uint session) => new(true, pid, session, null);
    }

    /// <summary>Sentinel returned by WTSGetActiveConsoleSessionId when no user is at the
    /// physical console (locked at the logon screen counts as a session; fully logged off
    /// does not).</summary>
    private const uint NoActiveSession = 0xFFFFFFFF;

    /// <summary>Launch <paramref name="applicationPath"/> with <paramref name="arguments"/>
    /// as SYSTEM in the active console session, on winsta0\default.</summary>
    public static InjectionResult Launch(string applicationPath, string arguments)
    {
        uint session = WTSGetActiveConsoleSessionId();
        if (session == NoActiveSession)
            return InjectionResult.Fail(
                "no interactive console session (no user is signed in), nothing to capture");

        // Not fatal on its own -- SYSTEM may already have SeTcb enabled -- but keep the
        // reason, since a later SetTokenInformation failure is otherwise a mystery.
        TryEnableTcbPrivilege(out var privError);

        if (!OpenProcessToken(GetCurrentProcess(),
                              TokenDuplicate | TokenQuery | TokenAssignPrimary |
                              TokenAdjustDefault | TokenAdjustSessionId,
                              out var processToken))
            return InjectionResult.Fail($"OpenProcessToken failed (win32 {LastError()})");

        IntPtr dupToken = IntPtr.Zero;
        IntPtr environment = IntPtr.Zero;
        try
        {
            if (!DuplicateTokenEx(processToken, MaximumAllowed, IntPtr.Zero,
                                  SecurityImpersonation, TokenPrimary, out dupToken))
                return InjectionResult.Fail($"DuplicateTokenEx failed (win32 {LastError()})");

            uint target = session;
            if (!SetTokenInformation(dupToken, TokenSessionIdClass, ref target, sizeof(uint)))
                return InjectionResult.Fail(
                    $"SetTokenInformation(TokenSessionId={session}) failed (win32 {LastError()})" +
                    (privError is null ? "" : $"; {privError}"));

            // Best-effort environment block for the target token; not fatal if it fails.
            if (!CreateEnvironmentBlock(out environment, dupToken, false))
                environment = IntPtr.Zero;

            var startupInfo = new STARTUPINFO
            {
                cb = Marshal.SizeOf<STARTUPINFO>(),
                // The capture happens on the interactive window station's default desktop;
                // phase 5 retargets to Winlogon when the secure desktop is active.
                lpDesktop = @"winsta0\default",
            };

            var commandLine = new StringBuilder();
            // argv[0] must be the (quoted) program path; CreateProcessAsUser may write into
            // this buffer, so it has to be mutable (StringBuilder), never a string literal.
            commandLine.Append('"').Append(applicationPath).Append('"');
            if (!string.IsNullOrEmpty(arguments))
                commandLine.Append(' ').Append(arguments);

            uint flags = CreateUnicodeEnvironment | CreateNoWindow;

            if (!CreateProcessAsUserW(
                    dupToken, applicationPath, commandLine,
                    IntPtr.Zero, IntPtr.Zero, false, flags,
                    environment, null, ref startupInfo, out var procInfo))
                return InjectionResult.Fail($"CreateProcessAsUser failed (win32 {LastError()})");

            // We don't wait on the helper -- it runs the session and exits on its own.
            if (procInfo.hThread != IntPtr.Zero) CloseHandle(procInfo.hThread);
            if (procInfo.hProcess != IntPtr.Zero) CloseHandle(procInfo.hProcess);
            return InjectionResult.Success(procInfo.dwProcessId, session);
        }
        finally
        {
            if (environment != IntPtr.Zero) DestroyEnvironmentBlock(environment);
            if (dupToken != IntPtr.Zero) CloseHandle(dupToken);
            CloseHandle(processToken);
        }
    }

    private static int LastError() => Marshal.GetLastWin32Error();

    // --- SeTcbPrivilege (needed to retarget the token's session id) --------------------
    private static bool TryEnableTcbPrivilege(out string? error)
    {
        error = null;
        if (!OpenProcessToken(GetCurrentProcess(),
                              TokenAdjustPrivileges | TokenQuery, out var token))
        {
            error = $"OpenProcessToken for SeTcb failed (win32 {LastError()})";
            return false;
        }
        try
        {
            if (!LookupPrivilegeValueW(null, "SeTcbPrivilege", out long luid))
            {
                error = $"LookupPrivilegeValue(SeTcb) failed (win32 {LastError()})";
                return false;
            }
            var priv = new TokenPrivileges
            {
                PrivilegeCount = 1,
                Luid = luid,
                Attributes = SePrivilegeEnabled,
            };
            if (!AdjustTokenPrivileges(token, false, ref priv, 0, IntPtr.Zero, IntPtr.Zero))
            {
                error = $"AdjustTokenPrivileges(SeTcb) failed (win32 {LastError()})";
                return false;
            }
            // AdjustTokenPrivileges reports success even when it granted nothing.
            int err = LastError();
            if (err != 0)
            {
                error = $"SeTcbPrivilege not held (win32 {err})";
                return false;
            }
            return true;
        }
        finally
        {
            CloseHandle(token);
        }
    }

    // --- P/Invoke ---------------------------------------------------------------------
    private const uint TokenAssignPrimary = 0x0001;
    private const uint TokenDuplicate = 0x0002;
    private const uint TokenQuery = 0x0008;
    private const uint TokenAdjustPrivileges = 0x0020;
    private const uint TokenAdjustDefault = 0x0080;
    private const uint TokenAdjustSessionId = 0x0100;
    private const uint MaximumAllowed = 0x02000000;
    private const int SecurityImpersonation = 2;   // SECURITY_IMPERSONATION_LEVEL
    private const int TokenPrimary = 1;             // TOKEN_TYPE
    private const int TokenSessionIdClass = 12;     // TOKEN_INFORMATION_CLASS.TokenSessionId
    private const int SePrivilegeEnabled = 0x00000002;
    private const uint CreateUnicodeEnvironment = 0x00000400;
    private const uint CreateNoWindow = 0x08000000;

    [StructLayout(LayoutKind.Sequential)]
    private struct TokenPrivileges
    {
        public int PrivilegeCount;
        public long Luid;
        public int Attributes;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct STARTUPINFO
    {
        public int cb;
        public string? lpReserved;
        public string? lpDesktop;
        public string? lpTitle;
        public int dwX, dwY, dwXSize, dwYSize, dwXCountChars, dwYCountChars, dwFillAttribute;
        public int dwFlags;
        public short wShowWindow;
        public short cbReserved2;
        public IntPtr lpReserved2;
        public IntPtr hStdInput, hStdOutput, hStdError;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct PROCESS_INFORMATION
    {
        public IntPtr hProcess;
        public IntPtr hThread;
        public uint dwProcessId;
        public uint dwThreadId;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint WTSGetActiveConsoleSessionId();

    [DllImport("kernel32.dll")]
    private static extern IntPtr GetCurrentProcess();

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool OpenProcessToken(IntPtr processHandle, uint desiredAccess,
                                                out IntPtr tokenHandle);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool DuplicateTokenEx(
        IntPtr existingToken, uint desiredAccess, IntPtr tokenAttributes,
        int impersonationLevel, int tokenType, out IntPtr newToken);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool SetTokenInformation(
        IntPtr tokenHandle, int tokenInformationClass, ref uint tokenInformation,
        int tokenInformationLength);

    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool LookupPrivilegeValueW(string? systemName, string name,
                                                     out long luid);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool AdjustTokenPrivileges(IntPtr tokenHandle, bool disableAll,
                                                     ref TokenPrivileges newState,
                                                     int bufferLength, IntPtr previousState,
                                                     IntPtr returnLength);

    [DllImport("userenv.dll", SetLastError = true)]
    private static extern bool CreateEnvironmentBlock(out IntPtr environment, IntPtr token,
                                                      bool inherit);

    [DllImport("userenv.dll", SetLastError = true)]
    private static extern bool DestroyEnvironmentBlock(IntPtr environment);

    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CreateProcessAsUserW(
        IntPtr token, string? applicationName, StringBuilder commandLine,
        IntPtr processAttributes, IntPtr threadAttributes, bool inheritHandles,
        uint creationFlags, IntPtr environment, string? currentDirectory,
        ref STARTUPINFO startupInfo, out PROCESS_INFORMATION processInformation);
}
