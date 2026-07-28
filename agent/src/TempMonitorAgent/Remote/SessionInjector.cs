using System.Runtime.InteropServices;
using System.Text;
using Serilog;

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
    /// as SYSTEM in an interactive session, on winsta0\default.
    ///
    /// <paramref name="targetSession"/> pins a specific Windows session (the operator picked it
    /// from the session switcher). It is validated against the live enumeration first: the list
    /// the operator clicked may be seconds stale, and injecting into a session that has since
    /// ended fails in a much less legible way than falling back does.</summary>
    public static InjectionResult Launch(string applicationPath, string arguments,
                                         uint? targetSession = null)
    {
        uint session;
        if (targetSession is { } requested && IsUsableSession(requested))
        {
            session = requested;
            Log.Information("Injecting helper into operator-selected session {Session}", session);
        }
        else
        {
            if (targetSession is { } gone)
                Log.Warning("Requested session {Session} is no longer usable; auto-selecting", gone);
            session = FindInteractiveSession();
            if (session == NoActiveSession)
                return InjectionResult.Fail(
                    "no interactive session available on this machine, nothing to capture");
            Log.Information("Injecting helper into session {Session}", session);
        }

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

    /// <summary>True if <paramref name="session"/> is something we could inject into right now:
    /// it exists, it is not the services session, and it is not in a dead state.</summary>
    private static bool IsUsableSession(uint session)
    {
        if (session == 0 || session == NoActiveSession) return false;
        foreach (var s in SessionProbe.Enumerate())
            if (s.SessionId == session)
                return s.State is SessionProbe.WtsActive
                                or SessionProbe.WtsConnected
                                or SessionProbe.WtsDisconnected;
        return false;
    }

    /// <summary>
    /// Pick the session that actually has a rendered desktop to capture.
    ///
    /// WTSGetActiveConsoleSessionId alone is NOT enough: it names the *physical console*
    /// session, which on an RDP-administered box is typically signed out and sitting at
    /// WTSConnected with no user and no composited desktop. Injecting there yields a helper
    /// that runs happily, fails Desktop Duplication (no output to duplicate), falls back to
    /// GDI, and streams a perfectly black 1920x1080 screen -- connected, encoding, showing
    /// nothing. Meanwhile the operator's real desktop is in the RDP session next door.
    ///
    /// Three tiers, in order:
    ///   1. A session in WTSActive -- a logged-on user attached to a desktop (a locked console
    ///      counts, it is still Active). The console wins ties so a physically-present user
    ///      beats a stray RDP session; otherwise the lowest active id, which is deterministic.
    ///   2. Failing that, the CONSOLE session if it exists at all in Connected/Disconnected
    ///      with a real window station. That is a machine sitting at the logon screen -- which
    ///      used to be refused outright, making it impossible to sign in remotely to a machine
    ///      nobody was signed in to. This is the whole point of remote-controlling a headless
    ///      box, so it must be reachable. Only ever the console: falling back to an arbitrary
    ///      disconnected RDP session would reintroduce the black-screen regression above.
    ///   3. Nothing usable -- reported to the operator.
    /// </summary>
    private static uint FindInteractiveSession()
    {
        uint console = SessionProbe.ConsoleSessionId();

        var sessions = SessionProbe.Enumerate();
        if (sessions.Count == 0)
        {
            Log.Warning("Session enumeration returned nothing (win32 {Err}); falling back to " +
                        "console session {Session}", LastError(), console);
            return console;
        }

        bool consoleIsActive = false;
        uint best = NoActiveSession;
        foreach (var s in sessions)
        {
            // Session 0 is the non-interactive services session -- never a capture target.
            if (s.SessionId == 0 || s.State != SessionProbe.WtsActive) continue;
            if (s.SessionId == console) consoleIsActive = true;
            if (best == NoActiveSession || s.SessionId < best) best = s.SessionId;
        }

        if (consoleIsActive)
        {
            Log.Information("Session select: console session {Session} is active", console);
            return console;
        }
        if (best != NoActiveSession)
        {
            Log.Information("Session select: no active console, using active session {Session}", best);
            return best;
        }

        // Tier 2: the logon screen.
        foreach (var s in sessions)
        {
            if (s.SessionId != console || s.SessionId == 0) continue;
            if (s.State is not (SessionProbe.WtsConnected or SessionProbe.WtsDisconnected)) continue;
            if (string.IsNullOrWhiteSpace(s.StationName)) continue;
            Log.Information(
                "Session select: nobody signed in; using console session {Session} at the logon " +
                "screen (station {Station}, state {State})", s.SessionId, s.StationName, s.State);
            return s.SessionId;
        }

        return NoActiveSession;
    }

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
