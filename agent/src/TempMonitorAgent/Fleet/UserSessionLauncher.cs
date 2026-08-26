using System.Runtime.InteropServices;
using System.Text;

namespace TempMonitorAgent.Fleet;

/// <summary>
/// Starts a program in a logged-on user's Windows session, AS that user.
///
/// This is what makes "Restart" mean something for an application. The agent is a service in
/// session 0, so a plain Process.Start puts the program in a session with no desktop, owned
/// by SYSTEM -- an Outlook launched that way has no user profile, no mapped drives, no access
/// to the mailbox it exists to open, and no window anybody can see. The user's own token,
/// their environment block and their session are all required for the relaunch to be the same
/// program the operator just ended.
///
/// **Deliberately NOT <see cref="TempMonitorAgent.Remote.SessionInjector"/>**, which solves
/// the neighbouring problem in the opposite way: it retargets the SERVICE's SYSTEM token into
/// an interactive session, because the remote-control helper has to be SYSTEM in order to
/// reach the secure desktop. Here that would be precisely wrong -- it would hand a user's
/// application SYSTEM privileges on their own desktop.
///
/// WTSQueryUserToken needs SE_TCB_NAME and CreateProcessAsUser needs SeAssignPrimaryToken +
/// SeIncreaseQuota. A service running as LocalSystem holds all three.
///
/// Everything fails soft with a described error rather than throwing: the caller has already
/// ended the process by the time this runs, so the operator must be told exactly which half
/// of the restart worked.
/// </summary>
internal static class UserSessionLauncher
{
    internal readonly record struct LaunchResult(bool Ok, uint Pid, string? Error)
    {
        public static LaunchResult Fail(string error) => new(false, 0, error);
        public static LaunchResult Success(uint pid) => new(true, pid, null);
    }

    /// <summary>Launch <paramref name="applicationPath"/> in <paramref name="session"/> as
    /// whoever is signed in there.
    ///
    /// <paramref name="arguments"/> is appended after the quoted program path, already
    /// escaped by the caller. It exists for open_item, which cannot always launch the thing
    /// the operator clicked: a document has to be handed to explorer.exe and a .ps1 to
    /// powershell.exe, and both need the path as an argument. A restart, the original
    /// caller, still passes nothing.</summary>
    internal static LaunchResult LaunchAsSessionUser(
        string applicationPath, string workingDirectory, uint session, string arguments = "")
    {
        // No user token means nobody is signed in there any more -- the session is at the
        // logon screen, or it ended between the sample and the click. There is nothing to
        // impersonate and nowhere for the window to go, so say that rather than falling back
        // to a SYSTEM launch the operator did not ask for and would not see.
        if (!WTSQueryUserToken(session, out var userToken))
            return LaunchResult.Fail(
                $"nobody is signed in to session {session} any more (win32 {LastError()})");

        IntPtr primaryToken = IntPtr.Zero;
        IntPtr environment = IntPtr.Zero;
        try
        {
            // WTSQueryUserToken hands back an impersonation token; CreateProcessAsUser needs
            // a primary one.
            if (!DuplicateTokenEx(userToken, MaximumAllowed, IntPtr.Zero,
                                  SecurityImpersonation, TokenPrimary, out primaryToken))
                return LaunchResult.Fail($"DuplicateTokenEx failed (win32 {LastError()})");

            // The user's own environment, not the service's: %APPDATA%, %TEMP% and the
            // profile path all differ, and a program started with SYSTEM's copy writes its
            // settings somewhere the user will never see them again.
            if (!CreateEnvironmentBlock(out environment, primaryToken, false))
                environment = IntPtr.Zero;

            var startupInfo = new STARTUPINFO
            {
                cb = Marshal.SizeOf<STARTUPINFO>(),
                lpDesktop = @"winsta0\default",
            };

            // argv[0] must be the quoted program path, and CreateProcessAsUser may write
            // into this buffer -- so it has to be mutable, never a string literal.
            var commandLine = new StringBuilder();
            commandLine.Append('"').Append(applicationPath).Append('"');
            if (!string.IsNullOrWhiteSpace(arguments))
                commandLine.Append(' ').Append(arguments);

            uint flags = CreateUnicodeEnvironment | CreateNewConsole;

            if (!CreateProcessAsUserW(
                    primaryToken, applicationPath, commandLine,
                    IntPtr.Zero, IntPtr.Zero, false, flags, environment,
                    string.IsNullOrEmpty(workingDirectory) ? null : workingDirectory,
                    ref startupInfo, out var procInfo))
                return LaunchResult.Fail($"CreateProcessAsUser failed (win32 {LastError()})");

            // We do not wait on it: this is the user's application, and its lifetime is
            // theirs. Both handles are closed so the agent does not hold the process open.
            if (procInfo.hThread != IntPtr.Zero) CloseHandle(procInfo.hThread);
            if (procInfo.hProcess != IntPtr.Zero) CloseHandle(procInfo.hProcess);
            return LaunchResult.Success(procInfo.dwProcessId);
        }
        finally
        {
            if (environment != IntPtr.Zero) DestroyEnvironmentBlock(environment);
            if (primaryToken != IntPtr.Zero) CloseHandle(primaryToken);
            CloseHandle(userToken);
        }
    }

    private static int LastError() => Marshal.GetLastWin32Error();

    // --- P/Invoke ---------------------------------------------------------------------
    private const uint MaximumAllowed = 0x02000000;
    private const int SecurityImpersonation = 2;    // SECURITY_IMPERSONATION_LEVEL
    private const int TokenPrimary = 1;              // TOKEN_TYPE
    private const uint CreateUnicodeEnvironment = 0x00000400;
    // A new console rather than CREATE_NO_WINDOW: a restarted GUI application does not use
    // it either way, and a console program that IS restarted should land somewhere visible
    // in the user's session rather than writing into a handle nobody holds.
    private const uint CreateNewConsole = 0x00000010;

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

    [DllImport("wtsapi32.dll", SetLastError = true)]
    private static extern bool WTSQueryUserToken(uint sessionId, out IntPtr token);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool DuplicateTokenEx(
        IntPtr existingToken, uint desiredAccess, IntPtr tokenAttributes,
        int impersonationLevel, int tokenType, out IntPtr newToken);

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
