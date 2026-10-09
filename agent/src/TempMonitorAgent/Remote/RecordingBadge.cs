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
/// when it was created. A thread that owns a window can never move desktops again --
/// <c>SetThreadDesktop</c> fails with ERROR_BUSY for the life of the thread (see
/// <see cref="ConsentBanner"/>'s threading note) -- so the badge cannot follow a desktop
/// switch by moving. Instead, every time the input desktop changes while recording, a new
/// throwaway thread binds to the new desktop and puts up its own window there. The windows on
/// desktops that are no longer shown stay up: they cost nothing, and they are already in place
/// when the user unlocks.
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
    private readonly object _gate = new();
    private readonly Dictionary<string, BadgeWindow> _windows = new(StringComparer.Ordinal);
    private string? _recordingId;

    /// <summary>Raised when the badge could not be put on a desktop the session moved to,
    /// with the desktop's name and why. The helper reports it to the hub, which ends the
    /// recording: a recording whose badge is not on screen is what the owner ruled out.</summary>
    public event Action<string, string>? Failed;

    public RecordingBadge(InputDesktopWatcher desktops, Action<string> log)
    {
        _desktops = desktops;
        _log = log;
        _desktops.Changed += OnDesktopChanged;
    }

    /// <summary>The recording the badge is up for, or null when it is down.</summary>
    public string? RecordingId
    {
        get { lock (_gate) return _recordingId; }
    }

    /// <summary>Put the badge up on the current input desktop for <paramref name="recordingId"/>.
    /// Blocks until the window exists or <see cref="ShowTimeoutMs"/> passes. Returns null when
    /// it is on screen, else why not.</summary>
    public string? Show(string recordingId)
    {
        lock (_gate) _recordingId = recordingId;
        string? error = EnsureOnInputDesktop();
        if (error is null)
            _log($"Recording badge shown for recording {recordingId}.");
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
            string? error = EnsureOnInputDesktop();
            if (error is not null && RecordingId is not null)
            {
                _log($"Recording badge could not be shown on desktop {to}: {error}");
                try { Failed?.Invoke(to, error); } catch { /* a handler must not kill this */ }
            }
        })
        {
            Name = "recording-badge-follow",
            IsBackground = true,
        };
        thread.Start();
    }

    private string? EnsureOnInputDesktop()
    {
        lock (_gate)
        {
            if (_recordingId is null) return null;
            if (_windows.TryGetValue(_desktops.Name, out var current) && current.IsAlive)
                return null;
        }

        var window = BadgeWindow.Start(_desktops.Name);
        if (!window.WaitReady(ShowTimeoutMs))
        {
            window.Close();
            return window.Error ?? "the badge window did not appear in time";
        }
        lock (_gate)
        {
            string desktop = window.Desktop ?? _desktops.Name;
            bool unwanted = _recordingId is null
                            || (_windows.TryGetValue(desktop, out var other) && other.IsAlive);
            if (!unwanted)
            {
                _windows[desktop] = window;
                return null;
            }
        }
        // Hidden while this one was starting, or a racing switch put one up first.
        window.Close();
        return null;
    }

    public void Dispose()
    {
        _desktops.Changed -= OnDesktopChanged;
        Hide();
    }

    /// <summary>One badge window on one desktop, with the thread that owns it.</summary>
    private sealed class BadgeWindow
    {
        /// <summary>DESKTOP_READOBJECTS | DESKTOP_CREATEWINDOW | DESKTOP_WRITEOBJECTS.</summary>
        private const uint BadgeDesktopAccess = 0x0001 | 0x0002 | 0x0080;

        private readonly ManualResetEventSlim _ready = new(false);
        private string InputDesktopName { get; init; } = "";
        private BadgeForm? _form;
        private volatile bool _alive;

        public string? Error { get; private set; }
        public string? Desktop { get; private set; }
        public bool IsAlive => _alive;

        public static BadgeWindow Start(string inputDesktopName)
        {
            var window = new BadgeWindow { InputDesktopName = inputDesktopName };
            var thread = new Thread(window.Run)
            {
                Name = "recording-badge",
                IsBackground = true,
            };
            thread.SetApartmentState(ApartmentState.STA);
            thread.Start();
            return window;
        }

        public bool WaitReady(int timeoutMs) => _ready.Wait(timeoutMs) && Error is null;

        public void Close()
        {
            var form = _form;
            if (form is null) return;
            try
            {
                if (form.IsHandleCreated) form.BeginInvoke(new Action(form.Close));
            }
            catch (InvalidOperationException) { /* already closing */ }
        }

        private void Run()
        {
            // Attach BEFORE anything creates a window on this thread -- afterwards it is too
            // late, for good. The handle is deliberately never closed: after the message loop
            // ends the thread still has a queue, so it cannot be detached first, and closing a
            // desktop a thread is attached to is undefined. One handle per desktop switch
            // during a recording, released when the helper exits with its session.
            //
            // Only the rights a window needs. GENERIC_ALL (what capture asks for) is refused to
            // anything that is not SYSTEM, which made the badge self-test fail from an ordinary
            // console; DESKTOP_CREATEWINDOW is the one right a badge cannot do without.
            IntPtr desktop = Desktops.OpenInputDesktop(0, false, BadgeDesktopAccess);
            if (desktop == IntPtr.Zero)
            {
                int err = Marshal.GetLastWin32Error();
                // Already on the input desktop (an unprivileged process on its own Default
                // desktop): nothing to attach to, and the window can go up right here.
                // An empty name is a watcher that cannot read the input desktop at all, which
                // happens only outside SYSTEM; then the user's own Default desktop is the one
                // place an unprivileged process can be showing anything.
                string? here = Desktops.CurrentThreadDesktopName();
                string expected = InputDesktopName.Length == 0 ? "Default" : InputDesktopName;
                if (here is null || !string.Equals(here, expected, StringComparison.Ordinal))
                {
                    Fail($"could not open the input desktop (win32 {err})");
                    return;
                }
                Desktop = here;
            }
            else if (!Desktops.SetThreadDesktop(desktop))
            {
                int err = Marshal.GetLastWin32Error();
                Desktops.CloseDesktop(desktop);
                Fail($"could not attach to the input desktop (win32 {err})");
                return;
            }
            else
            {
                Desktop = Desktops.NameOf(desktop);
            }

            try
            {
                ConsentBanner.InitialiseUi();
                _form = new BadgeForm();
                _form.Shown += (_, _) =>
                {
                    _alive = true;
                    _ready.Set();
                };
                _form.FormClosed += (_, _) => _alive = false;
                Application.Run(_form);
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
