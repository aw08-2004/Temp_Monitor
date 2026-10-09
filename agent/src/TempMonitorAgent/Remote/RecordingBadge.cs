using System.Drawing;
using System.Drawing.Drawing2D;
using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace TempMonitorAgent.Remote;

/// <summary>
/// The "Recording Screen" badge (roadmap #19): a small topmost label in the top-left corner of
/// the PC's screen for as long as an operator is recording the session.
///
/// <b>Why it exists.</b> The owner's decision is that a recording is never silent: the person
/// at the desk can always see that their screen is being recorded, including on the lock
/// screen and at the logon screen with nobody signed in. The hub enforces the "never" half --
/// it stores no video until this class has reported the badge as shown (recordings.py), and it
/// ends the recording when this class reports a desktop it could not show the badge on.
///
/// <b>One window per desktop.</b> The lock and logon screens are a different desktop
/// (Winlogon) from the user's (Default), and a window lives on the desktop its thread was on
/// when it was created. A thread that owns a window cannot move desktops --
/// <c>SetThreadDesktop</c> fails with ERROR_BUSY for as long as it owns one, which for a UI
/// thread is for good (see <see cref="ConsentBanner"/>'s threading note) -- so the badge
/// cannot follow a desktop switch by moving. Instead, every time the input desktop changes
/// while recording, a new throwaway thread binds to the new desktop and puts up its own window
/// there. The windows on desktops that are no longer shown stay up: they cost nothing, and
/// they are already in place when the user unlocks.
///
/// <b>The badge thread is born MTA and becomes an STA only after it has attached.</b> An STA
/// cannot be relied on to attach. The first STA in a process owns COM's hidden window from the
/// moment it starts, before any of its code runs; any other STA gets one at its first pumping
/// wait, which every managed wait on an STA is. The badge thread was always the helper's first
/// STA, and that is what ended every recording on AIO-HOBBY with "could not attach to the input
/// desktop (win32 170)". Attaching while still MTA does not depend on which STA COM counts as
/// first, or on what the thread has waited on; the details, and the alternatives rejected, are
/// in <c>BadgeWindow.Run</c>.
///
/// <b>Click-through and never focused.</b> It must not steal a keystroke from the user or the
/// operator, so it is a no-activate tool window with WS_EX_TRANSPARENT. It IS in the captured
/// picture, deliberately: the recording then shows the badge the user saw. *Rejected:*
/// excluding it from capture with WDA_EXCLUDEFROMCAPTURE, which would make the video the one
/// place the badge is missing.
/// </summary>
public sealed class RecordingBadge : IDisposable
{
    private const int ShowTimeoutMs = 5000;

    private readonly InputDesktopWatcher _desktops;
    private readonly Action<string> _log;
    private readonly Func<string, BadgeWindow> _startWindow;
    private readonly object _gate = new();
    private readonly Dictionary<string, BadgeWindow> _windows = new(StringComparer.Ordinal);
    private string? _recordingId;

    /// <summary>Raised when the badge could not be put on a desktop the session moved to,
    /// with the desktop's name and why. The helper reports it to the hub, which ends the
    /// recording: a recording whose badge is not on screen is what the owner ruled out.</summary>
    public event Action<string, string>? Failed;

    public RecordingBadge(InputDesktopWatcher desktops, Action<string> log)
        : this(desktops, log, name => BadgeWindow.Start(name))
    {
    }

    /// <summary>With <paramref name="startWindow"/> in place of
    /// <see cref="BadgeWindow.Start(string)"/>, so RecordingBadgeTests can hand
    /// <see cref="Show"/> a window that attaches to a private desktop -- the one way to check,
    /// without SYSTEM, that the desktop a failed badge tried is what Show hands back for the
    /// hub.</summary>
    internal RecordingBadge(InputDesktopWatcher desktops, Action<string> log,
                            Func<string, BadgeWindow> startWindow)
    {
        _desktops = desktops;
        _log = log;
        _startWindow = startWindow;
        _desktops.Changed += OnDesktopChanged;
    }

    /// <summary>The recording the badge is up for, or null when it is down.</summary>
    public string? RecordingId
    {
        get { lock (_gate) return _recordingId; }
    }

    /// <summary>Put the badge up on the current input desktop for <paramref name="recordingId"/>.
    /// Blocks until the window exists or <see cref="ShowTimeoutMs"/> passes. Returns null when
    /// it is on screen, else why not. <paramref name="desktop"/> is the desktop it is on, or
    /// the one it could not be put on -- reported beside the error, because "win32 170" alone
    /// does not say whether it was the user's desktop or the lock screen.</summary>
    public string? Show(string recordingId, out string? desktop)
    {
        lock (_gate) _recordingId = recordingId;
        (string? error, desktop) = EnsureOnInputDesktop();
        if (error is null)
            _log($"Recording badge shown for recording {recordingId} on desktop {desktop ?? "(unknown)"}.");
        return error;
    }

    /// <summary>Take the badge down everywhere.</summary>
    public void Hide()
    {
        List<BadgeWindow> windows;
        lock (_gate)
        {
            if (_recordingId is not null)
                _log($"Recording badge hidden for recording {_recordingId}.");
            _recordingId = null;
            windows = _windows.Values.ToList();
            _windows.Clear();
        }
        foreach (var window in windows) window.Close();
    }

    private void OnDesktopChanged(string from, string to)
    {
        // The watcher's thread must not block or attach, so the work goes to a thread of its
        // own -- which is also the thread that must not be the one that ends up owning a window.
        if (RecordingId is null) return;
        var thread = new Thread(() =>
        {
            var (error, tried) = EnsureOnInputDesktop();
            if (error is not null && RecordingId is not null)
            {
                // The desktop the badge actually tried beats the watcher's earlier reading, if
                // the input desktop moved again in between.
                string desktop = tried ?? to;
                _log($"Recording badge could not be shown on desktop {desktop}: {error}");
                try { Failed?.Invoke(desktop, error); } catch { /* a handler must not kill this */ }
            }
        })
        {
            Name = "recording-badge-follow",
            IsBackground = true,
        };
        thread.Start();
    }

    /// <summary>Returns the error (null when the badge is up) and the desktop it is on or was
    /// trying for.</summary>
    private (string? Error, string? Desktop) EnsureOnInputDesktop()
    {
        string input = _desktops.Name;
        lock (_gate)
        {
            if (_recordingId is null) return (null, null);
            if (_windows.TryGetValue(input, out var current) && current.IsAlive)
                return (null, input);
        }

        var window = _startWindow(input);
        if (!window.WaitReady(ShowTimeoutMs))
        {
            // Read before Close(): the reason is the badge's own, or the timeout's.
            string error = window.Error ?? "the badge window did not appear in time";
            string? tried = window.Desktop ?? (input.Length == 0 ? null : input);
            // Also takes down a window that is still on its way up (BadgeWindow.Close).
            window.Close();
            return (error, tried);
        }
        lock (_gate)
        {
            string desktop = window.Desktop ?? input;
            bool unwanted = _recordingId is null
                            || (_windows.TryGetValue(desktop, out var other) && other.IsAlive);
            if (!unwanted)
            {
                _windows[desktop] = window;
                return (null, desktop);
            }
        }
        // Hidden while this one was starting, or a racing switch put one up first.
        window.Close();
        return (null, window.Desktop);
    }

    public void Dispose()
    {
        _desktops.Changed -= OnDesktopChanged;
        Hide();
    }

    /// <summary>One badge window on one desktop, with the thread that owns it. Internal rather
    /// than private only so RecordingBadgeTests can drive it against a private desktop of its
    /// own -- the one way to exercise the attach without SYSTEM and without a window on the
    /// screen of whoever runs the tests.</summary>
    internal sealed class BadgeWindow
    {
        /// <summary>DESKTOP_READOBJECTS | DESKTOP_CREATEWINDOW | DESKTOP_WRITEOBJECTS.</summary>
        internal const uint BadgeDesktopAccess = 0x0001 | 0x0002 | 0x0080;

        private readonly ManualResetEventSlim _ready = new(false);
        // Guards _form and _closeRequested together, so a Close() can never fall between the
        // badge thread publishing its form and that form's window coming up.
        private readonly object _gate = new();
        private string InputDesktopName { get; init; } = "";
        private Func<IntPtr> OpenDesktop { get; init; } = () => IntPtr.Zero;
        private Action<BadgeWindow>? BeforeRun { get; init; }
        private Thread? _thread;
        private BadgeForm? _form;
        private bool _closeRequested;
        private volatile bool _alive;

        public string? Error { get; private set; }

        /// <summary>The desktop the badge is on -- or, when it failed, the one it was trying
        /// for, which is what the hub shows beside the error. Null only when nothing knew.</summary>
        public string? Desktop { get; private set; }

        public bool IsAlive => _alive;

        /// <summary>True once this thread is attached to its desktop through
        /// <c>SetThreadDesktop</c>, as opposed to having stayed where it started.</summary>
        public bool Attached { get; private set; }

        /// <summary>The badge's window, once it has been shown.</summary>
        public IntPtr WindowHandle { get; private set; }

        public static BadgeWindow Start(string inputDesktopName) =>
            Start(inputDesktopName, () => Desktops.OpenInputDesktop(0, false, BadgeDesktopAccess));

        /// <summary>Start against whatever desktop <paramref name="openDesktop"/> opens. It runs
        /// on the badge thread, and a zero return means "could not open one".
        ///
        /// <paramref name="beforeRun"/> also runs on the badge thread, after the form is
        /// published and before its window exists: the one moment a <see cref="Close"/> can
        /// only be honoured by the Shown handler. Production never passes it. Without it a test
        /// cannot land a Close() there, and every other moment is caught earlier, so that half
        /// of the orphan fix could be deleted with every test still green.</summary>
        internal static BadgeWindow Start(string inputDesktopName, Func<IntPtr> openDesktop,
                                          Action<BadgeWindow>? beforeRun = null)
        {
            var window = new BadgeWindow
            {
                InputDesktopName = inputDesktopName,
                OpenDesktop = openDesktop,
                BeforeRun = beforeRun,
            };
            var thread = new Thread(window.Run)
            {
                Name = "recording-badge",
                IsBackground = true,
            };
            // MTA at birth, NOT STA, though this is a UI thread: Run attaches to the desktop
            // first and only then becomes an STA. See the note in Run for why the order is the
            // whole fix.
            thread.SetApartmentState(ApartmentState.MTA);
            window._thread = thread;
            thread.Start();
            return window;
        }

        public bool WaitReady(int timeoutMs) => _ready.Wait(timeoutMs) && Error is null;

        /// <summary>True once the badge's thread has ended, its window with it.</summary>
        public bool WaitExited(int timeoutMs) => _thread?.Join(timeoutMs) ?? true;

        /// <summary>Take this badge down, including one that has not appeared yet.
        ///
        /// The not-yet case is the one that matters. A caller that gave up in
        /// <see cref="WaitReady"/> closes the window and reports the badge as failed, and the
        /// hub ends the recording. This used to do nothing when the form or its window did not
        /// exist yet, so the badge then came up anyway, a moment late, and stayed on screen for
        /// the rest of the session: in no list Hide or Dispose would reach, and topmost again
        /// every two seconds. The flag below is what Run checks before the window exists and
        /// again when it is shown.</summary>
        public void Close()
        {
            BadgeForm? form;
            lock (_gate)
            {
                _closeRequested = true;
                form = _form;
                // No window yet: Run sees the flag, at the latest when the form is shown.
                if (form is null || !form.IsHandleCreated) return;
            }
            try { form.BeginInvoke(new Action(form.Close)); }
            catch (InvalidOperationException) { /* already closing */ }
        }

        /// <summary>The badge thread: attach, become an STA, then show the window until it is
        /// closed. Each step fails closed through <see cref="Fail"/>, and the order of the first
        /// two is the whole fix for win32 170 -- see <see cref="AttachToDesktop"/>.</summary>
        private void Run()
        {
            if (!AttachToDesktop() || !BecomeSingleThreaded()) return;
            ShowUntilClosed();
        }

        /// <summary>Put this thread on the badge's desktop. False, with the reason recorded,
        /// when it cannot.</summary>
        private bool AttachToDesktop()
        {
            // Attach BEFORE this thread owns a window of any kind -- while it owns one,
            // SetThreadDesktop fails with ERROR_BUSY. That includes a window no code here
            // creates. A thread started as an STA has the runtime call
            // CoInitializeEx(APARTMENTTHREADED) before Run's first line, and the first STA in a
            // process -- which the badge thread always is, in the helper -- gets COM's hidden
            // OleMainThreadWndClass window right then. So the badge thread used to be born
            // unable to attach to anything, even a fresh handle to the desktop it was already
            // on, and every recording on AIO-HOBBY ended "could not attach to the input desktop
            // (win32 170)". The window is message-only, so EnumThreadWindows does not list it;
            // that is how it hid. Not being the first STA is no way out: a later STA has no
            // window at birth, but gets its own at its first pumping wait, so it attaches only
            // until it has waited on anything. Hence the order: born MTA (no window), attach,
            // and only then become the STA that WinForms expects (BecomeSingleThreaded).
            //
            // The handle is deliberately never closed: after the message loop ends the thread
            // still has a queue, so it cannot be detached first, and closing a desktop a thread
            // is attached to is undefined. One handle per desktop switch during a recording,
            // released when the helper exits with its session.
            //
            // Only the rights a window needs. GENERIC_ALL (what capture asks for) is more than
            // a window needs and is not granted to every caller; DESKTOP_CREATEWINDOW is the one
            // right a badge cannot do without.
            IntPtr desktop = OpenDesktop();
            if (desktop == IntPtr.Zero) return StayOnCurrentDesktop(Marshal.GetLastWin32Error());

            // Named before the attach, so a failure can say which desktop it was: the
            // user's own (Default) and the lock screen (Winlogon) are different problems.
            Desktop = Desktops.NameOf(desktop) ?? (InputDesktopName.Length == 0 ? null : InputDesktopName);
            if (!Desktops.SetThreadDesktop(desktop))
            {
                int err = Marshal.GetLastWin32Error();
                Desktops.CloseDesktop(desktop);
                Fail($"could not attach to the input desktop (win32 {err})");
                return false;
            }
            Attached = true;
            return true;
        }

        /// <summary>The input desktop could not be opened (<paramref name="openError"/>): go up
        /// where this thread already is, if that is the desktop being asked for.</summary>
        private bool StayOnCurrentDesktop(int openError)
        {
            // Already on the input desktop (an unprivileged process on its own Default
            // desktop): nothing to attach to, and the window can go up right here.
            // An empty name is a watcher that cannot read the input desktop at all, which
            // happens only outside SYSTEM; then the user's own Default desktop is the one
            // place an unprivileged process can be showing anything.
            // This branch never calls SetThreadDesktop, so a self-test that takes it says
            // nothing about the attach every recording as SYSTEM depends on.
            string? here = Desktops.CurrentThreadDesktopName();
            string expected = InputDesktopName.Length == 0 ? "Default" : InputDesktopName;
            if (here is null || !string.Equals(here, expected, StringComparison.Ordinal))
            {
                Desktop = InputDesktopName.Length == 0 ? null : InputDesktopName;
                Fail($"could not open the input desktop (win32 {openError})");
                return false;
            }
            Desktop = here;
            return true;
        }

        /// <summary>Turn this thread, now attached, into the STA WinForms expects. False, with
        /// the reason recorded, when the runtime refuses.</summary>
        private bool BecomeSingleThreaded()
        {
            // Now the STA. Unknown first: the runtime made this thread an explicit MTA when it
            // started and will not put STA on top of that (TrySetApartmentState(STA) returns
            // false; CoInitializeEx says RPC_E_CHANGED_MODE). Unknown makes the runtime
            // CoUninitialize, after which STA takes -- and COM's window is created now, on the
            // badge's own desktop, where it does no harm.
            //
            // *Rejected:* no apartment at start and STA after the attach -- on .NET 10 a thread
            // with no apartment set is an MTA from birth, so that quietly leaves the form on
            // MTA. Staying on MTA on purpose -- WinForms does run this form there, since nothing
            // in it needs OLE, but the first clipboard, drag-drop or dialog anyone adds would
            // then throw. Keeping the STA start and dropping to Unknown before the attach --
            // it works, but undoes COM set-up the runtime already did, where this never does
            // it in the first place. A long-lived STA thread kept around so the badge thread is
            // never the process's first one -- it works only by the accident of which STA COM
            // treats as main, and one exit brings the bug back.
            Thread self = Thread.CurrentThread;
            if (self.TrySetApartmentState(ApartmentState.Unknown) &&
                self.TrySetApartmentState(ApartmentState.STA))
                return true;
            // Fail closed, like every other badge failure: the hub ends the recording.
            Fail("could not make the badge thread single-threaded after attaching " +
                 $"(apartment {self.GetApartmentState()})");
            return false;
        }

        /// <summary>Create the badge's form and run its message loop until it closes -- unless a
        /// <see cref="Close"/> already arrived, in which case it is never shown.</summary>
        private void ShowUntilClosed()
        {
            try
            {
                ConsentBanner.InitialiseUi();
                var form = new BadgeForm();
                form.Shown += (_, _) => OnShown(form);
                form.FormClosed += (_, _) => _alive = false;
                lock (_gate)
                {
                    // Taken down while still attaching: never show it. No Fail() -- the caller
                    // already gave up and reported its own reason, which must not be replaced.
                    if (_closeRequested)
                    {
                        form.Dispose();
                        return;
                    }
                    _form = form;
                }
                BeforeRun?.Invoke(this);
                Application.Run(form);
            }
            catch (Exception e)
            {
                Fail(e.Message);
            }
            finally
            {
                _alive = false;
            }
        }

        private void OnShown(BadgeForm form)
        {
            // The second half of Close()'s contract: a close that arrived after the form
            // was published but before its window existed is honoured here.
            bool late;
            lock (_gate)
            {
                late = _closeRequested;
                if (!late) _alive = true;
            }
            if (late)
            {
                form.Close();
                return;
            }
            WindowHandle = form.Handle;
            _ready.Set();
        }

        private void Fail(string error)
        {
            Error = error;
            _ready.Set();
        }
    }

    /// <summary>The badge itself: a red, rounded pill reading "Recording Screen".</summary>
    private sealed class BadgeForm : Form
    {
        private const string Caption = "Recording Screen";
        private const int EdgeMargin = 12;
        private const int ReassertMs = 2000;

        private const int WS_EX_TOPMOST = 0x00000008;
        private const int WS_EX_TRANSPARENT = 0x00000020;
        private const int WS_EX_TOOLWINDOW = 0x00000080;
        private const int WS_EX_NOACTIVATE = 0x08000000;
        private static readonly IntPtr HWND_TOPMOST = new(-1);
        private const uint SWP_NOSIZE = 0x0001;
        private const uint SWP_NOMOVE = 0x0002;
        private const uint SWP_NOACTIVATE = 0x0010;
        private const uint SWP_SHOWWINDOW = 0x0040;

        private readonly System.Windows.Forms.Timer _reassert = new() { Interval = ReassertMs };
        private readonly Font _font = new("Segoe UI", 10f, FontStyle.Bold);

        public BadgeForm()
        {
            FormBorderStyle = FormBorderStyle.None;
            ShowInTaskbar = false;
            TopMost = true;
            StartPosition = FormStartPosition.Manual;
            BackColor = Color.FromArgb(196, 32, 38);
            ForeColor = Color.White;
            // Opacity makes the window layered, which WS_EX_TRANSPARENT needs to let clicks
            // fall through to whatever is underneath.
            Opacity = 0.92;
            DoubleBuffered = true;

            Size textSize = TextRenderer.MeasureText(Caption, _font);
            int height = textSize.Height + 12;
            ClientSize = new Size(textSize.Width + height + 10, height);
            Rectangle area = Screen.PrimaryScreen?.WorkingArea ?? new Rectangle(0, 0, 800, 600);
            Location = new Point(area.Left + EdgeMargin, area.Top + EdgeMargin);
            using var path = Pill(new Rectangle(Point.Empty, ClientSize));
            Region = new Region(path);

            // Something else that is also topmost and newer (a full-screen app, the logon UI)
            // would otherwise cover it after a while.
            _reassert.Tick += (_, _) =>
                SetWindowPos(Handle, HWND_TOPMOST, 0, 0, 0, 0,
                             SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW);
            _reassert.Start();
        }

        protected override bool ShowWithoutActivation => true;

        protected override CreateParams CreateParams
        {
            get
            {
                var cp = base.CreateParams;
                cp.ExStyle |= WS_EX_TOPMOST | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE;
                return cp;
            }
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            int h = ClientSize.Height;
            int dot = h / 3;
            using (var white = new SolidBrush(Color.White))
                e.Graphics.FillEllipse(white, h / 2 - dot / 2, h / 2 - dot / 2, dot, dot);
            var textArea = new Rectangle(h, 0, ClientSize.Width - h - 6, h);
            TextRenderer.DrawText(e.Graphics, Caption, _font, textArea, ForeColor,
                                  TextFormatFlags.VerticalCenter | TextFormatFlags.Left);
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                _reassert.Dispose();
                _font.Dispose();
            }
            base.Dispose(disposing);
        }

        private static GraphicsPath Pill(Rectangle r)
        {
            var path = new GraphicsPath();
            int d = r.Height;
            path.AddArc(r.Left, r.Top, d, d, 90, 180);
            path.AddArc(r.Right - d, r.Top, d, d, 270, 180);
            path.CloseFigure();
            return path;
        }

        [DllImport("user32.dll", SetLastError = true)]
        private static extern bool SetWindowPos(IntPtr hWnd, IntPtr insertAfter, int x, int y,
                                                int cx, int cy, uint flags);
    }
}
