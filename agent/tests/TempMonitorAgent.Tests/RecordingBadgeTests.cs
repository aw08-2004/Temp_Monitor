using System.Runtime.InteropServices;
using System.Text.Json;
using TempMonitorAgent.Remote;

namespace TempMonitorAgent.Tests;

/// <summary>
/// The recording badge's move onto the desktop it has to be seen on (roadmap #19).
///
/// **The silent failure is a self-test that passes while every recording fails.** In the
/// helper the badge thread runs as SYSTEM, opens the input desktop and attaches to it with
/// <c>SetThreadDesktop</c>. It used to be born an STA, and the first STA in a process owns
/// COM's hidden message-only window before its first line runs -- so that attach failed with
/// win32 170 on every attempt, and every recording on AIO-HOBBY ended "could not attach to the
/// input desktop". The console self-test meant to catch it could pass without ever reaching
/// <c>SetThreadDesktop</c>: where it could not open the input desktop it showed the badge on
/// the desktop it was already on.
///
/// So these tests make the badge MOVE. Each creates a private desktop of its own in the current
/// window station -- CreateDesktop needs no privilege, and SwitchDesktop is never called, so
/// nothing appears on the screen of whoever runs them -- and hands the badge an opener for it
/// in place of OpenInputDesktop. It stands in for the input desktop; it is not production's
/// exact case. On an unlocked PC the helper attaches to a fresh handle of the very desktop it
/// is already on (Default), and on a locked one to Winlogon. Both failed the same way while the
/// thread owned COM's window, so a different desktop reproduces the cause, and it also proves
/// the window really moved (EnumDesktopWindows), which a same-desktop attach could not.
///
/// The attach test checks the badge thread's apartment at the moment it attaches, not only that
/// the attach worked, because whether an STA can attach depends on the whole process. The first
/// STA owns COM's hidden window from birth; a later one gets its own only at its first pumping
/// wait. So on the STA-born code the attach itself succeeds whenever some other STA is alive in
/// the test host -- a leaked badge thread, or a parallel test class with a UI thread of its own
/// -- and the test would pass on the broken code. The apartment fails it either way.
/// (BadgeTakenDownBeforeItAppearsNeverComesUp went red on that code too, but only because its
/// opener's gate.Wait() is a pumping wait; it is not the guard.) Every test still takes its
/// badge thread down and joins it.
/// </summary>
public class RecordingBadgeTests
{
    private const int ReadyMs = 10_000;
    private const int ExitMs = 5_000;
    private const int ERROR_BUSY = 170;

    [Fact]
    public void BadgeAttachesToAnotherDesktopAndShowsThere()
    {
        using var desktop = PrivateDesktop.Create();
        var window = RecordingBadge.BadgeWindow.Start(desktop.Name, desktop.OpenForBadge);
        try
        {
            Assert.True(window.WaitReady(ReadyMs),
                        $"the badge did not come up on {desktop.Name}: {window.Error ?? "timed out"}");
            Assert.True(desktop.ApartmentAtOpen == ApartmentState.MTA,
                        $"the badge thread was {desktop.ApartmentAtOpen} when it attached; an STA " +
                        "attaches only while some other STA is COM's first and it has not waited " +
                        "on anything, which in the helper it never is");
            Assert.True(window.Attached, "the badge showed without attaching to its desktop");
            Assert.Equal(desktop.Name, window.Desktop);
            Assert.True(desktop.HasWindow(window.WindowHandle),
                        "the badge's window is not on the desktop it attached to");
        }
        finally
        {
            TakeDown(window, desktop);
        }
        Assert.True(window.WaitExited(0), "the badge's thread outlived Close()");
        Assert.False(window.IsAlive);
    }

    /// <summary>What RecordingBadge does when the badge is slow: stop waiting after
    /// ShowTimeoutMs, report the failure, and take the window down. A window still on its way
    /// up must then never appear -- it would be on screen for the rest of the session with
    /// nothing left holding it, after the hub had already ended the recording.</summary>
    [Fact]
    public void BadgeTakenDownBeforeItAppearsNeverComesUp()
    {
        using var desktop = PrivateDesktop.Create();
        using var gate = new ManualResetEventSlim(false);
        var window = RecordingBadge.BadgeWindow.Start(desktop.Name, () =>
        {
            gate.Wait();
            return desktop.OpenForBadge();
        });
        try
        {
            Assert.False(window.WaitReady(200));
            window.Close();
            gate.Set();

            Assert.True(window.WaitExited(ExitMs),
                        "the badge came up after it was taken down, and nothing can reach it now");
            // It got as far as its desktop, so the close -- not a failed attach -- is what
            // kept it down.
            Assert.True(window.Attached, $"the badge never attached: {window.Error}");
            Assert.Equal(IntPtr.Zero, window.WindowHandle);
            Assert.False(window.IsAlive);
        }
        finally
        {
            gate.Set();
            TakeDown(window, desktop);
        }
    }

    /// <summary>The other half of the same race. A Close() that lands after the badge thread
    /// has published its form but before the form's window exists finds nothing to post a
    /// close to, so only the Shown handler can honour it. The test above closes while the
    /// thread is still in its opener, which the check before publishing catches; without the
    /// hook here, nothing reaches this moment and the Shown check could go unnoticed.</summary>
    [Fact]
    public void BadgeClosedAsItsWindowIsBeingMadeNeverStaysUp()
    {
        using var desktop = PrivateDesktop.Create();
        bool closedThere = false;
        var window = RecordingBadge.BadgeWindow.Start(desktop.Name, desktop.OpenForBadge, badge =>
        {
            badge.Close();
            closedThere = true;
        });
        try
        {
            Assert.True(window.WaitExited(ExitMs),
                        "the badge stayed up after a close that came while its window was being made");
            Assert.True(closedThere, $"the badge never reached the moment under test: {window.Error}");
            Assert.True(window.Attached, $"the badge never attached: {window.Error}");
            Assert.Null(window.Error);
            Assert.Equal(IntPtr.Zero, window.WindowHandle);
            Assert.False(window.IsAlive);
            Assert.False(window.WaitReady(0), "a badge that was taken down reported itself ready");
        }
        finally
        {
            TakeDown(window, desktop);
        }
    }

    /// <summary>A failed attach has to say which desktop it was, because that is what tells
    /// the user's own desktop apart from the lock screen in the hub's end_detail.</summary>
    [Fact]
    public void FailedAttachSaysWhichDesktopItTried()
    {
        using var desktop = PrivateDesktop.Create();
        var window = RecordingBadge.BadgeWindow.Start(desktop.Name, () =>
        {
            MakeThreadBusy();
            return desktop.OpenForBadge();
        });
        try
        {
            Assert.False(window.WaitReady(ReadyMs));
            Assert.Equal($"could not attach to the input desktop (win32 {ERROR_BUSY})", window.Error);
            Assert.Equal(desktop.Name, window.Desktop);
            Assert.False(window.Attached);
        }
        finally
        {
            TakeDown(window, desktop);
        }
    }

    /// <summary>The same desktop name, the rest of the way to the hub. The test above stops at
    /// the badge window; the "desktop": null the hub saw had a second cause past it, a literal
    /// null where the helper built its report. This one goes through RecordingBadge.Show and
    /// RemoteHelper.ShowBadge to the payload the helper posts, with a watcher that has never
    /// read a name -- so the desktop can only come from the badge that tried it.</summary>
    [Fact]
    public void FailedShowTellsTheHubWhichDesktop()
    {
        using var desktop = PrivateDesktop.Create();
        using var watcher = new InputDesktopWatcher(1000, _ => { });   // never started: Name is ""
        RecordingBadge.BadgeWindow? started = null;
        using var badge = new RecordingBadge(watcher, _ => { }, name =>
            started = RecordingBadge.BadgeWindow.Start(name, () =>
            {
                MakeThreadBusy();
                return desktop.OpenForBadge();
            }));
        try
        {
            RemoteHelper.BadgeReport report = RemoteHelper.ShowBadge(badge, "rec-1");

            Assert.Equal("failed", report.State);
            Assert.Equal($"could not attach to the input desktop (win32 {ERROR_BUSY})", report.Error);
            Assert.Equal(desktop.Name, report.Desktop);
            Assert.Null(badge.RecordingId);   // a failed badge is taken down, not left pending

            JsonElement payload = JsonSerializer.SerializeToElement(
                report.Payload(), new JsonSerializerOptions(JsonSerializerDefaults.Web));
            Assert.Equal("rec-1", payload.GetProperty("recording_id").GetString());
            Assert.Equal("failed", payload.GetProperty("badge").GetString());
            Assert.Equal(report.Error, payload.GetProperty("error").GetString());
            Assert.Equal(desktop.Name, payload.GetProperty("desktop").GetString());
        }
        finally
        {
            if (started is not null) TakeDown(started, desktop);
        }
    }

    /// <summary>Gives the calling thread a window before the badge attaches: the documented
    /// ERROR_BUSY case, whatever the thread's apartment. Message-only, so never on screen, and
    /// destroyed with the thread.</summary>
    private static void MakeThreadBusy() =>
        Native.CreateWindowExW(0, "Static", "", 0, 0, 0, 0, 0,
                               Native.HWND_MESSAGE, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero);

    private static void TakeDown(RecordingBadge.BadgeWindow window, PrivateDesktop desktop)
    {
        window.Close();
        // The badge keeps its desktop handle for as long as its thread lives (a thread cannot
        // be detached from its desktop first), so the handle is closed here once that thread
        // is gone. A failed attach closed it already.
        if (window.WaitExited(ExitMs) && window.Attached) desktop.ReleaseBadgeHandle();
    }

    /// <summary>A desktop of the test's own, never switched to and so never on screen.</summary>
    private sealed class PrivateDesktop : IDisposable
    {
        private const uint GENERIC_ALL = 0x10000000;

        private readonly IntPtr _created;
        private IntPtr _badge;

        public string Name { get; }

        /// <summary>The badge thread's apartment when it opened this desktop, which is just
        /// before it attaches. Null until the badge has opened it.</summary>
        public ApartmentState? ApartmentAtOpen { get; private set; }

        private PrivateDesktop(string name, IntPtr created)
        {
            Name = name;
            _created = created;
        }

        public static PrivateDesktop Create()
        {
            string name = "fh-badge-test-" + Guid.NewGuid().ToString("n")[..12];
            IntPtr created = Native.CreateDesktopW(name, IntPtr.Zero, IntPtr.Zero, 0, GENERIC_ALL,
                                                   IntPtr.Zero);
            if (created == IntPtr.Zero)
                throw new InvalidOperationException(
                    $"CreateDesktop({name}) failed (win32 {Marshal.GetLastWin32Error()})");
            return new PrivateDesktop(name, created);
        }

        /// <summary>The badge's opener: a handle with only the badge's own rights, as
        /// OpenInputDesktop gives it in production. Runs on the badge thread.</summary>
        public IntPtr OpenForBadge()
        {
            ApartmentAtOpen = Thread.CurrentThread.GetApartmentState();
            _badge = Native.OpenDesktopW(Name, 0, false, RecordingBadge.BadgeWindow.BadgeDesktopAccess);
            return _badge;
        }

        public void ReleaseBadgeHandle()
        {
            if (_badge == IntPtr.Zero) return;
            Desktops.CloseDesktop(_badge);
            _badge = IntPtr.Zero;
        }

        /// <summary>True when <paramref name="hwnd"/> is a top-level window on this desktop.</summary>
        public bool HasWindow(IntPtr hwnd)
        {
            if (hwnd == IntPtr.Zero) return false;
            bool found = false;
            Native.EnumWindowsProc proc = (h, _) =>
            {
                if (h != hwnd) return true;
                found = true;
                return false;
            };
            Native.EnumDesktopWindows(_created, proc, IntPtr.Zero);
            GC.KeepAlive(proc);
            return found;
        }

        public void Dispose() => Desktops.CloseDesktop(_created);
    }

    private static class Native
    {
        internal static readonly IntPtr HWND_MESSAGE = new(-3);

        internal delegate bool EnumWindowsProc(IntPtr hwnd, IntPtr lParam);

        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        internal static extern IntPtr CreateDesktopW(string name, IntPtr device, IntPtr devmode,
                                                     uint flags, uint access, IntPtr attributes);

        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        internal static extern IntPtr OpenDesktopW(string name, uint flags, bool inherit, uint access);

        [DllImport("user32.dll", SetLastError = true)]
        internal static extern bool EnumDesktopWindows(IntPtr desktop, EnumWindowsProc proc,
                                                       IntPtr lParam);

        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        internal static extern IntPtr CreateWindowExW(int exStyle, string className, string title,
                                                      int style, int x, int y, int width, int height,
                                                      IntPtr parent, IntPtr menu, IntPtr instance,
                                                      IntPtr param);
    }
}
