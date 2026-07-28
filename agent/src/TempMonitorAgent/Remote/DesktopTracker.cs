using System.Runtime.InteropServices;
using System.Text;

namespace TempMonitorAgent.Remote;

/// <summary>
/// Follows the Windows <b>input desktop</b> so capture and input keep working across the
/// Default -> Winlogon switch (lock screen, logon screen, UAC secure desktop).
///
/// Why this exists: a window station holds several desktops, and only one of them is the
/// "input desktop" at any moment. Three things silently do the wrong thing when the calling
/// thread is attached to a different desktop than the input one:
///   * <c>IDXGIOutput1::DuplicateOutput</c> returns ACCESS_DENIED, so Desktop Duplication
///     rebuilds forever and never recovers -- which is exactly why locking the screen used to
///     kill the stream permanently.
///   * <c>SendInput</c> posts into the wrong desktop's queue, so keystrokes vanish.
///   * <c>GetDC(NULL)</c> hands back the calling thread's desktop DC, so the GDI fallback
///     blits a blank screen.
///
/// The split into two types is not decoration. <c>SetThreadDesktop</c> is <b>per-thread</b>,
/// and a desktop handle must never be closed while it is set on any thread. Sharing one handle
/// between the capture and input threads makes that impossible to reason about, so the watcher
/// only ever *queries* (opening and closing its handle within one call, never attaching) and
/// each bound thread owns its own handle through a <see cref="ThreadDesktopBinder"/>.
///
/// Requires the process to be SYSTEM on the interactive window station -- which is precisely
/// what <see cref="SessionInjector"/> arranges. A user-token process is denied Winlogon by
/// design and will simply keep its original attachment.
/// </summary>
public sealed class InputDesktopWatcher : IDisposable
{
    private readonly int _pollMs;
    private readonly Action<string> _log;
    private readonly CancellationTokenSource _cts = new();
    private Thread? _thread;

    private long _generation;
    private string _name = "";
    private int _consecutiveFailures;

    /// <summary>Bumps every time the input desktop's NAME changes. Bound threads compare this
    /// against what they last saw, which keeps the per-frame check to one volatile read.</summary>
    public long Generation => Interlocked.Read(ref _generation);

    /// <summary>Last observed input-desktop name ("Default", "Winlogon", "Screen-saver", ...).
    /// Empty until the first successful poll.</summary>
    public string Name => Volatile.Read(ref _name);

    /// <summary>Raised on the watcher thread when the input desktop changes. Handlers must not
    /// block and must not call SetThreadDesktop (this thread is deliberately unattached).</summary>
    public event Action<string, string>? Changed;

    public InputDesktopWatcher(int pollMs, Action<string> log)
    {
        _pollMs = pollMs <= 0 ? 250 : pollMs;
        _log = log;
    }

    public void Start()
    {
        if (_thread is not null) return;
        _thread = new Thread(Loop) { Name = "remote-desktop-watch", IsBackground = true };
        _thread.Start();
    }

    private void Loop()
    {
        while (!_cts.IsCancellationRequested)
        {
            Poll();
            try { Task.Delay(_pollMs, _cts.Token).Wait(_cts.Token); }
            catch (OperationCanceledException) { break; }
            catch (AggregateException) { break; }
        }
    }

    private void Poll()
    {
        // GENERIC_READ is enough to read the name, and this handle is closed immediately --
        // the watcher never attaches, so it never has to worry about close-while-attached.
        IntPtr desktop = Desktops.OpenInputDesktop(0, false, Desktops.GENERIC_READ);
        if (desktop == IntPtr.Zero)
        {
            // Transient during a switch; entirely normal. Only complain if it persists.
            if (++_consecutiveFailures == 20)
                _log($"OpenInputDesktop has failed {_consecutiveFailures} times running " +
                     $"(win32 {Marshal.GetLastWin32Error()}); still using desktop '{Name}'");
            return;
        }
        try
        {
            string? name = Desktops.NameOf(desktop);
            _consecutiveFailures = 0;
            if (string.IsNullOrEmpty(name)) return;

            string previous = Volatile.Read(ref _name);
            if (string.Equals(name, previous, StringComparison.Ordinal)) return;

            Volatile.Write(ref _name, name);
            Interlocked.Increment(ref _generation);
            try { Changed?.Invoke(previous, name); }
            catch { /* a diagnostic handler must never take the watcher down */ }
        }
        finally
        {
            Desktops.CloseDesktop(desktop);
        }
    }

    public void Dispose()
    {
        _cts.Cancel();
        try { _thread?.Join(1000); } catch { }
        _cts.Dispose();
    }
}

/// <summary>
/// Keeps ONE thread attached to the current input desktop.
///
/// Must be constructed on the thread it binds, and that thread must own no windows, hooks or
/// message queues -- <c>SetThreadDesktop</c> fails with ERROR_BUSY otherwise, permanently, for
/// the life of the thread. That is why the capture and input loops run on dedicated threads
/// rather than the thread pool: a pool thread that once created a window (the consent banner
/// used to) poisons every later attempt to attach.
/// </summary>
public sealed class ThreadDesktopBinder : IDisposable
{
    private const int ERROR_BUSY = 170;

    private readonly string _role;
    private readonly Action<string> _log;
    private readonly IntPtr _original;

    private IntPtr _held = IntPtr.Zero;
    private long _seenGeneration = -1;
    private bool _loggedBusy;

    /// <summary>Name of the desktop this thread is currently attached to, or null before the
    /// first successful attach.</summary>
    public string? AttachedName { get; private set; }

    /// <summary>Consecutive <see cref="SyncTo"/> calls that could not attach. Non-zero means the
    /// thread is still on its previous desktop.</summary>
    public int ConsecutiveFailures { get; private set; }

    public ThreadDesktopBinder(string role, Action<string> log)
    {
        _role = role;
        _log = log;
        // Not ours to close -- GetThreadDesktop returns a borrowed handle. Kept only so Dispose
        // can put the thread back where it found it.
        _original = Desktops.GetThreadDesktop(Desktops.GetCurrentThreadId());
    }

    /// <summary>Attach to the current input desktop if the watcher has seen a change (or we
    /// have never attached). Returns true only when this call actually switched desktops, which
    /// is the caller's cue to rebuild anything desktop-bound (the DXGI duplication, the monitor
    /// geometry). On failure the thread keeps its existing attachment.</summary>
    public bool SyncTo(InputDesktopWatcher? watcher)
    {
        if (watcher is null) return false;

        long generation = watcher.Generation;
        if (generation == _seenGeneration && _held != IntPtr.Zero) return false;

        // GENERIC_ALL first: the capture path needs more than read access on some drivers.
        IntPtr fresh = Desktops.OpenInputDesktop(0, false, Desktops.GENERIC_ALL);
        if (fresh == IntPtr.Zero)
            fresh = Desktops.OpenInputDesktop(0, false, Desktops.DESKTOP_READ_WRITE);
        if (fresh == IntPtr.Zero)
        {
            ConsecutiveFailures++;
            return false;
        }

        if (!Desktops.SetThreadDesktop(fresh))
        {
            int err = Marshal.GetLastWin32Error();
            Desktops.CloseDesktop(fresh);
            ConsecutiveFailures++;
            if (err == ERROR_BUSY && !_loggedBusy)
            {
                _loggedBusy = true;
                _log($"remote-{_role} thread owns windows or hooks, so it cannot follow the " +
                     "input desktop; it will stay on " + (AttachedName ?? "its original desktop") +
                     ". This is a code defect, not an environment problem.");
            }
            return false;
        }

        // Safe to release the previous handle only now that the thread no longer holds it.
        IntPtr previous = _held;
        _held = fresh;
        _seenGeneration = generation;
        ConsecutiveFailures = 0;
        if (previous != IntPtr.Zero) Desktops.CloseDesktop(previous);

        string? was = AttachedName;
        AttachedName = Desktops.NameOf(fresh) ?? watcher.Name;
        // First attach isn't a "switch" worth shouting about; later ones are the interesting event.
        if (was is not null && !string.Equals(was, AttachedName, StringComparison.Ordinal))
            _log($"remote-{_role} thread followed desktop {was} -> {AttachedName}");
        return true;
    }

    public void Dispose()
    {
        // Restore first, THEN close: closing a desktop still set on this thread is undefined.
        if (_original != IntPtr.Zero)
            try { Desktops.SetThreadDesktop(_original); } catch { }
        if (_held != IntPtr.Zero)
        {
            Desktops.CloseDesktop(_held);
            _held = IntPtr.Zero;
        }
    }
}

/// <summary>Desktop/window-station P/Invoke shared by the watcher, the binder, and the
/// <c>--desktop-probe</c> diagnostic.</summary>
internal static class Desktops
{
    internal const uint GENERIC_READ = 0x80000000;
    internal const uint GENERIC_ALL = 0x10000000;
    /// <summary>DESKTOP_READOBJECTS | DESKTOP_WRITEOBJECTS | DESKTOP_SWITCHDESKTOP -- the
    /// minimum a capture/input thread needs when GENERIC_ALL is refused.</summary>
    internal const uint DESKTOP_READ_WRITE = 0x0001 | 0x0080 | 0x0100;

    private const int UOI_NAME = 2;

    /// <summary>Name of a desktop handle ("Default", "Winlogon", ...), or null.</summary>
    internal static string? NameOf(IntPtr desktop)
    {
        if (desktop == IntPtr.Zero) return null;
        var buffer = new byte[256];
        if (!GetUserObjectInformationW(desktop, UOI_NAME, buffer, buffer.Length, out int needed))
            return null;
        // needed includes the trailing NUL; two bytes of it in UTF-16.
        return Encoding.Unicode.GetString(buffer, 0, Math.Clamp(needed - 2, 0, buffer.Length));
    }

    /// <summary>Name of the desktop the calling thread is attached to right now.</summary>
    internal static string? CurrentThreadDesktopName()
    {
        IntPtr desktop = GetThreadDesktop(GetCurrentThreadId());
        return desktop == IntPtr.Zero ? null : NameOf(desktop);
    }

    [DllImport("user32.dll", SetLastError = true)]
    internal static extern IntPtr OpenInputDesktop(uint flags, bool inherit, uint desiredAccess);

    [DllImport("user32.dll", SetLastError = true)]
    internal static extern bool SetThreadDesktop(IntPtr desktop);

    [DllImport("user32.dll", SetLastError = true)]
    internal static extern bool CloseDesktop(IntPtr desktop);

    [DllImport("user32.dll", SetLastError = true)]
    internal static extern IntPtr GetThreadDesktop(uint threadId);

    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    internal static extern bool GetUserObjectInformationW(
        IntPtr obj, int index, byte[] info, int length, out int lengthNeeded);

    [DllImport("kernel32.dll")]
    internal static extern uint GetCurrentThreadId();
}
