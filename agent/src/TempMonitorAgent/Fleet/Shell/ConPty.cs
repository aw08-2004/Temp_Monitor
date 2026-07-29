using System.ComponentModel;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

namespace TempMonitorAgent.Fleet.Shell;

/// <summary>
/// A child process attached to a Windows pseudoconsole (ConPTY), plus the two pipes that
/// carry its console I/O.
///
/// WHY THIS EXISTS. The older <see cref="ShellSession"/> drives powershell.exe over plain
/// redirected stdin/stdout. That is fine for running a script and collecting its text, but
/// the child can SEE that it has no console, and everything an operator means by
/// "interactive" lives on the other side of that line: `Read-Host` writes its prompt through
/// the host UI, `$Host.UI.PromptForChoice` and `[Console]::ReadKey` want a console input
/// buffer, tab-completion needs to redraw the line, progress bars and colour need cursor
/// addressing. Under redirected pipes those either vanish or misbehave -- which is exactly
/// what makes a redirected shell useless for driving an installer.
///
/// A pseudoconsole gives the child a real console device. It gets a console window handle, a
/// screen buffer, a cursor -- and in exchange the "output" we read is no longer lines of
/// text but a VT/ANSI byte stream (SGR colour, cursor moves, erase-in-line, alternate screen
/// buffer). That stream is meant for a terminal emulator, which is why the console side
/// renders it with xterm.js rather than appending to a &lt;pre&gt;.
///
/// INTEROP NOTES, each of which is a way to hang or leak if you get it wrong:
///
///  * Four pipe ends, two of which we must CLOSE. CreatePseudoConsole duplicates the handles
///    it is given, so the parent's copies of the child ends (input-read, output-write) have
///    to be closed once the process exists. Keep the output-write end open and the output
///    pipe NEVER reaches EOF when the child exits -- the read pump blocks forever and the
///    session is immortal.
///  * The pipes are deliberately NOT inheritable and CreateProcess is called with
///    bInheritHandles = false. The pseudoconsole is handed over through the thread attribute
///    list, not through handle inheritance; inheriting as well would hand a stray copy of
///    the pipe to every grandchild and defeat the EOF above.
///  * ClosePseudoConsole flushes the client's remaining output and can block until the
///    client is gone, so <see cref="Dispose"/> ends the process tree FIRST and only then
///    closes the pseudoconsole. The reverse order deadlocks whenever the child ignores the
///    close.
///  * The command line passed to CreateProcess must be a WRITABLE buffer (the API may modify
///    it in place), hence the StringBuilder.
/// </summary>
internal sealed class ConPtyProcess : IDisposable
{
    private IntPtr _hpcon;
    private SafeFileHandle? _inputWrite;
    private SafeFileHandle? _outputRead;
    private SafeProcessHandle? _process;
    private bool _disposed;

    /// <summary>Write keystrokes here; this is the child's console INPUT.</summary>
    public SafeFileHandle InputWrite => _inputWrite ?? throw new ObjectDisposedException(nameof(ConPtyProcess));

    /// <summary>Read the VT byte stream here; this is the child's console OUTPUT.</summary>
    public SafeFileHandle OutputRead => _outputRead ?? throw new ObjectDisposedException(nameof(ConPtyProcess));

    public int ProcessId { get; private set; }

    /// <summary>True once the hosted process has exited (or we have torn it down).</summary>
    public bool HasExited
    {
        get
        {
            if (_disposed || _process is null || _process.IsInvalid) return true;
            return GetExitCodeProcess(_process.DangerousGetHandle(), out var code) && code != STILL_ACTIVE;
        }
    }

    /// <summary>Exit code of the hosted process, or null while it is still running.</summary>
    public int? ExitCode
    {
        get
        {
            if (_process is null || _process.IsInvalid) return null;
            if (!GetExitCodeProcess(_process.DangerousGetHandle(), out var code)) return null;
            return code == STILL_ACTIVE ? null : (int)code;
        }
    }

    /// <summary>Launch <paramref name="commandLine"/> attached to a new pseudoconsole of the
    /// given size. Throws <see cref="Win32Exception"/> if any step fails; on failure nothing
    /// is left open.</summary>
    public static ConPtyProcess Start(string commandLine, string workingDirectory, short cols, short rows)
    {
        // A zero dimension makes the console API unhappy and a huge one wastes a screen
        // buffer, so clamp to something a browser could plausibly ask for.
        cols = Clamp(cols, 20, 500);
        rows = Clamp(rows, 5, 200);

        var session = new ConPtyProcess();
        SafeFileHandle? inputRead = null, outputWrite = null;
        var attributeList = IntPtr.Zero;

        try
        {
            if (!CreatePipe(out inputRead, out var inputWrite, IntPtr.Zero, 0))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "CreatePipe (input) failed");
            session._inputWrite = inputWrite;

            if (!CreatePipe(out var outputRead, out outputWrite, IntPtr.Zero, 0))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "CreatePipe (output) failed");
            session._outputRead = outputRead;

            var size = new COORD { X = cols, Y = rows };
            var hr = CreatePseudoConsole(size, inputRead, outputWrite, 0, out session._hpcon);
            if (hr != 0) throw new Win32Exception(hr, "CreatePseudoConsole failed");

            attributeList = BuildPseudoConsoleAttributeList(session._hpcon);

            var startupInfo = new STARTUPINFOEX
            {
                StartupInfo =
                {
                    cb = Marshal.SizeOf<STARTUPINFOEX>(),
                    // THE SUBTLE ONE. Without this the child inherits the AGENT's standard
                    // handles and writes its output there instead of to its console -- so
                    // the pseudoconsole attaches correctly (`mode con` reports our exact
                    // size) and yet almost nothing comes back down the pty, because only
                    // programs that write explicitly to CON are reaching it. Everything
                    // that goes through plain stdout, which is nearly everything, vanishes
                    // into whatever the service's stdout happens to be.
                    //
                    // STARTF_USESTDHANDLES with NULL handles gives the child nothing to
                    // inherit, so the console subsystem binds its std handles to the
                    // pseudoconsole -- which is what we wanted all along. (Temporarily
                    // clearing our own handles with SetStdHandle around CreateProcess also
                    // works, but mutates process-global state that other threads are
                    // logging through.)
                    dwFlags = STARTF_USESTDHANDLES,
                    hStdInput = IntPtr.Zero,
                    hStdOutput = IntPtr.Zero,
                    hStdError = IntPtr.Zero,
                },
                lpAttributeList = attributeList,
            };

            // Mutable buffer: CreateProcessW is documented to be able to modify this string.
            var mutableCommandLine = new System.Text.StringBuilder(commandLine);
            var created = CreateProcess(
                lpApplicationName: null,
                lpCommandLine: mutableCommandLine,
                lpProcessAttributes: IntPtr.Zero,
                lpThreadAttributes: IntPtr.Zero,
                bInheritHandles: false,
                dwCreationFlags: EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT,
                lpEnvironment: IntPtr.Zero,
                lpCurrentDirectory: string.IsNullOrWhiteSpace(workingDirectory) ? null : workingDirectory,
                lpStartupInfo: ref startupInfo,
                lpProcessInformation: out var processInfo);

            if (!created)
                throw new Win32Exception(Marshal.GetLastWin32Error(), "CreateProcess (ConPTY) failed");

            session.ProcessId = (int)processInfo.dwProcessId;
            session._process = new SafeProcessHandle(processInfo.hProcess, ownsHandle: true);
            CloseHandle(processInfo.hThread);

            // Tie the child to the agent's kill-on-close job, so a hard Environment.Exit for a
            // self-update can't orphan a SYSTEM shell (see ProcessTree).
            try { ProcessTree.AssignToKillOnCloseJob(processInfo.hProcess); } catch { }

            return session;
        }
        catch
        {
            session.Dispose();
            throw;
        }
        finally
        {
            // The pseudoconsole holds its own duplicates of these; ours must go or the output
            // pipe never signals EOF when the child exits. Safe to do here either way: by this
            // point the process is created, or we are unwinding.
            inputRead?.Dispose();
            outputWrite?.Dispose();
            if (attributeList != IntPtr.Zero)
            {
                DeleteProcThreadAttributeList(attributeList);
                Marshal.FreeHGlobal(attributeList);
            }
        }
    }

    /// <summary>Tell the pseudoconsole the terminal was resized, so the child re-wraps and
    /// redraws (this is what makes a maximised browser window usable). Best-effort.</summary>
    public void Resize(short cols, short rows)
    {
        if (_disposed || _hpcon == IntPtr.Zero) return;
        var size = new COORD { X = Clamp(cols, 20, 500), Y = Clamp(rows, 5, 200) };
        try { ResizePseudoConsole(_hpcon, size); } catch { /* best-effort */ }
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;

        // ORDER MATTERS: end the process tree before closing the pseudoconsole. See the
        // class remarks -- ClosePseudoConsole waits on the client and deadlocks otherwise.
        try { if (ProcessId > 0) ProcessTree.KillDescendants(ProcessId); } catch { }
        try
        {
            if (_process is { IsInvalid: false } && !HasExited)
                TerminateProcess(_process.DangerousGetHandle(), 1);
        }
        catch { }

        if (_hpcon != IntPtr.Zero)
        {
            try { ClosePseudoConsole(_hpcon); } catch { }
            _hpcon = IntPtr.Zero;
        }

        _inputWrite?.Dispose(); _inputWrite = null;
        _outputRead?.Dispose(); _outputRead = null;
        _process?.Dispose(); _process = null;
    }

    private static short Clamp(short value, short low, short high) =>
        value < low ? low : value > high ? high : value;

    /// <summary>Allocate a one-entry PROC_THREAD_ATTRIBUTE_LIST carrying the pseudoconsole.
    /// This is how the child is attached; it is NOT handle inheritance.</summary>
    private static IntPtr BuildPseudoConsoleAttributeList(IntPtr hpcon)
    {
        var size = IntPtr.Zero;
        // First call always "fails" with ERROR_INSUFFICIENT_BUFFER; it exists to report the size.
        InitializeProcThreadAttributeList(IntPtr.Zero, 1, 0, ref size);

        var list = Marshal.AllocHGlobal(size);
        try
        {
            if (!InitializeProcThreadAttributeList(list, 1, 0, ref size))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "InitializeProcThreadAttributeList failed");

            if (!UpdateProcThreadAttribute(
                    list, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE, hpcon,
                    (IntPtr)IntPtr.Size, IntPtr.Zero, IntPtr.Zero))
            {
                DeleteProcThreadAttributeList(list);
                throw new Win32Exception(Marshal.GetLastWin32Error(), "UpdateProcThreadAttribute failed");
            }
            return list;
        }
        catch
        {
            Marshal.FreeHGlobal(list);
            throw;
        }
    }

    // ---------------- P/Invoke ----------------

    private const uint EXTENDED_STARTUPINFO_PRESENT = 0x00080000;
    private const int STARTF_USESTDHANDLES = 0x00000100;
    private const uint CREATE_UNICODE_ENVIRONMENT = 0x00000400;
    private static readonly IntPtr PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = (IntPtr)0x00020016;
    private const uint STILL_ACTIVE = 259;

    [StructLayout(LayoutKind.Sequential)]
    private struct COORD
    {
        public short X;
        public short Y;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct STARTUPINFO
    {
        public int cb;
        public IntPtr lpReserved;
        public IntPtr lpDesktop;
        public IntPtr lpTitle;
        public int dwX, dwY, dwXSize, dwYSize, dwXCountChars, dwYCountChars, dwFillAttribute;
        public int dwFlags;
        public short wShowWindow;
        public short cbReserved2;
        public IntPtr lpReserved2;
        public IntPtr hStdInput, hStdOutput, hStdError;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct STARTUPINFOEX
    {
        public STARTUPINFO StartupInfo;
        public IntPtr lpAttributeList;
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
    private static extern bool CreatePipe(
        out SafeFileHandle hReadPipe, out SafeFileHandle hWritePipe, IntPtr lpPipeAttributes, int nSize);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern int CreatePseudoConsole(
        COORD size, SafeFileHandle hInput, SafeFileHandle hOutput, uint dwFlags, out IntPtr phPC);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern int ResizePseudoConsole(IntPtr hPC, COORD size);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern void ClosePseudoConsole(IntPtr hPC);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool InitializeProcThreadAttributeList(
        IntPtr lpAttributeList, int dwAttributeCount, int dwFlags, ref IntPtr lpSize);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool UpdateProcThreadAttribute(
        IntPtr lpAttributeList, uint dwFlags, IntPtr attribute, IntPtr lpValue,
        IntPtr cbSize, IntPtr lpPreviousValue, IntPtr lpReturnSize);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern void DeleteProcThreadAttributeList(IntPtr lpAttributeList);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CreateProcess(
        string? lpApplicationName,
        System.Text.StringBuilder lpCommandLine,
        IntPtr lpProcessAttributes,
        IntPtr lpThreadAttributes,
        bool bInheritHandles,
        uint dwCreationFlags,
        IntPtr lpEnvironment,
        string? lpCurrentDirectory,
        ref STARTUPINFOEX lpStartupInfo,
        out PROCESS_INFORMATION lpProcessInformation);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetExitCodeProcess(IntPtr hProcess, out uint lpExitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool TerminateProcess(IntPtr hProcess, uint uExitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr hObject);
}
