using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace TempMonitorAgent.Remote;

/// <summary>
/// Attended-consent prompt for a remote session (roadmap #2, phase 6). When the session's
/// consent mode is "attended", the logged-in user must approve before the operator can see or
/// drive their screen; "unattended" (the default) skips this and connects immediately.
///
/// The helper runs as SYSTEM inside the interactive session, so it can put a dialog on the
/// user's own desktop. We use MessageBoxTimeout so an unanswered prompt auto-DENIES after a
/// timeout rather than leaving a session hanging -- attended means someone actively agrees, and
/// "no answer" must fail closed, not open.
///
/// <b>Threading:</b> showing this prompt gives the calling thread a window, and a thread that
/// owns a window can never again call <c>SetThreadDesktop</c> -- it fails with ERROR_BUSY for
/// the life of the thread. That is why <see cref="RequestConsentAsync"/> runs the prompt on its
/// own dedicated, throwaway thread rather than the thread pool: a poisoned pool thread would go
/// back into the pool and silently break whichever desktop-bound loop landed on it later.
/// </summary>
public static class ConsentBanner
{
    private const int DefaultTimeoutSeconds = 30;

    // Visual styles and DPI mode are process-wide and must be set before the first window
    // exists, so they happen once, lazily, on whichever consent prompt comes first.
    private static int _uiInitialised;

    /// <summary>Show the prompt on a dedicated thread and await the answer. Always use this
    /// rather than wrapping <see cref="RequestConsent"/> in Task.Run.</summary>
    public static Task<bool> RequestConsentAsync(
        string machine, string operatorEmail, int timeoutSeconds = DefaultTimeoutSeconds)
    {
        var completion = new TaskCompletionSource<bool>(
            TaskCreationOptions.RunContinuationsAsynchronously);
        var thread = new Thread(() =>
        {
            // Deny on any failure, matching RequestConsent's fail-closed contract.
            try { completion.TrySetResult(RequestConsent(machine, operatorEmail, timeoutSeconds)); }
            catch { completion.TrySetResult(false); }
        })
        {
            Name = "remote-consent",
            IsBackground = true,
        };
        thread.SetApartmentState(ApartmentState.STA); // it is a UI thread, however briefly
        thread.Start();
        return completion.Task;
    }

    /// <summary>Ask the logged-in user to approve the session. Returns true only on an explicit
    /// Yes; a No, a timeout, or any failure to show the prompt denies (fail closed).
    ///
    /// Creates a window on the calling thread -- see the threading note on the class. Prefer
    /// <see cref="RequestConsentAsync"/>.</summary>
    public static bool RequestConsent(string machine, string operatorEmail, int timeoutSeconds = DefaultTimeoutSeconds)
    {
        var who = string.IsNullOrWhiteSpace(operatorEmail) ? "An IT operator" : operatorEmail;
        try
        {
            InitialiseUi();
            using var dialog = new ConsentDialog(machine, who, timeoutSeconds);
            dialog.ShowDialog();
            return dialog.Approved;
        }
        catch
        {
            // The styled dialog is the nice path, not the load-bearing one. If WinForms cannot
            // put a window up here (an unusual desktop, a GDI+ failure), fall back to the plain
            // system prompt rather than silently denying a session the user would have allowed.
            return RequestConsentFallback(machine, who, timeoutSeconds);
        }
    }

    private static void InitialiseUi()
    {
        if (Interlocked.Exchange(ref _uiInitialised, 1) != 0) return;
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        // Per-monitor DPI so the card is crisp on a scaled laptop panel. Non-fatal if the host
        // already fixed the mode -- the dialog just renders system-scaled.
        try { Application.SetHighDpiMode(HighDpiMode.PerMonitorV2); } catch { }
    }

    /// <summary>The original MessageBox prompt, kept as the last resort behind
    /// <see cref="ConsentDialog"/>. Same fail-closed contract.</summary>
    private static bool RequestConsentFallback(string machine, string who, int timeoutSeconds)
    {
        var text =
            $"{who} is requesting to view and control this computer ({machine}).\n\n" +
            "Do you want to allow this remote session?\n\n" +
            $"(If you do not respond within {timeoutSeconds} seconds, the request is denied.)";
        try
        {
            int result = MessageBoxTimeoutW(
                IntPtr.Zero, text, "Remote support request",
                MB_YESNO | MB_ICONQUESTION | MB_SYSTEMMODAL | MB_TOPMOST | MB_SETFOREGROUND,
                0, (uint)(timeoutSeconds * 1000));
            return result == IDYES;
        }
        catch
        {
            // Can't show a prompt (no desktop, API unavailable): deny, since attended consent
            // could not actually be obtained.
            return false;
        }
    }

    private const uint MB_YESNO = 0x00000004;
    private const uint MB_ICONQUESTION = 0x00000020;
    private const uint MB_SYSTEMMODAL = 0x00001000;
    private const uint MB_TOPMOST = 0x00040000;
    private const uint MB_SETFOREGROUND = 0x00010000;
    private const int IDYES = 6;

    // MessageBoxTimeoutW is an undocumented but long-stable user32 export (present on every
    // supported Windows), and is the clean way to get an auto-dismissing prompt. On timeout it
    // returns MB_TIMEDOUT (32000), which is not IDYES, so we correctly treat it as a denial.
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern int MessageBoxTimeoutW(
        IntPtr hWnd, string text, string caption, uint type, ushort languageId, uint milliseconds);
}
