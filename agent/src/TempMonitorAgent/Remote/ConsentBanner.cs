using System.Runtime.InteropServices;

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
/// </summary>
public static class ConsentBanner
{
    private const int DefaultTimeoutSeconds = 30;

    /// <summary>Ask the logged-in user to approve the session. Returns true only on an explicit
    /// Yes; a No, a timeout, or any failure to show the prompt denies (fail closed).</summary>
    public static bool RequestConsent(string machine, string operatorEmail, int timeoutSeconds = DefaultTimeoutSeconds)
    {
        var who = string.IsNullOrWhiteSpace(operatorEmail) ? "An IT operator" : operatorEmail;
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
