using System.Runtime.InteropServices;
using System.Text.Json;
using System.Windows.Forms;

namespace TempMonitorAgent.UserMessage;

/// <summary>
/// Entry point for the session-injected helper that shows a rule's message.
///
/// Same architecture as <see cref="Remote.RemoteHelper"/>, and for the same non-negotiable
/// reason: the agent is a Windows Service in <b>session 0</b>, which has no rendered desktop.
/// A dialog created there is created onto nothing -- no error, no window, just a prompt no
/// human can ever see. So the service launches THIS SAME BINARY with
/// <see cref="AgentConfig.ShowMessageArg"/> into the interactive session (see
/// <see cref="Remote.SessionInjector"/>), and this runs there.
///
/// <b>Threading:</b> the dialog is shown on a dedicated STA thread, exactly as
/// <see cref="Remote.ConsentBanner"/> does. A thread that owns a window can never again call
/// <c>SetThreadDesktop</c> -- it fails with ERROR_BUSY for the life of the thread -- so this
/// must never be a thread-pool thread, even though this process does nothing else. Copying
/// the discipline costs nothing and means the rule cannot be broken later by someone adding
/// desktop work to this helper.
///
/// The answer travels back through a file rather than an exit code: an exit code has room for
/// a number, and what the hub needs is which of up to four buttons was pressed plus when it
/// was shown and when it was answered.
/// </summary>
public static class MessageHelper
{
    private static int _uiInitialised;

    /// <summary>If this process was launched to show a message, return the request-file path
    /// that followed the flag. Null for a normal service launch, so Program.cs can branch
    /// before it ever builds the service host.</summary>
    public static string? TryGetRequestFileArg(string[] args)
    {
        for (int i = 0; i < args.Length; i++)
            if (string.Equals(args[i], AgentConfig.ShowMessageArg, StringComparison.Ordinal))
                return i + 1 < args.Length ? args[i + 1] : "";
        return null;
    }

    /// <summary>Show the message described by <paramref name="requestPath"/> and write the
    /// answer beside it. Always returns 0: the ANSWER FILE is the result, and a non-zero exit
    /// would make the service report a failed command for a dialog the user answered
    /// perfectly well.</summary>
    public static int Run(string requestPath)
    {
        var answer = new MessageAnswer
        {
            ShownAt = DateTimeOffset.UtcNow.ToUnixTimeSeconds(),
            Outcome = MessageOutcomes.Failed,
        };
        try
        {
            var request = JsonSerializer.Deserialize<MessageRequest>(File.ReadAllText(requestPath))
                          ?? throw new InvalidOperationException("empty message request");
            answer.Outcome = ShowOnDedicatedThread(request);
        }
        catch (Exception ex)
        {
            answer.Outcome = MessageOutcomes.Failed;
            answer.Error = ex.Message;
        }
        answer.RespondedAt = DateTimeOffset.UtcNow.ToUnixTimeSeconds();

        try
        {
            File.WriteAllText(AnswerPathFor(requestPath), JsonSerializer.Serialize(answer));
        }
        catch
        {
            // Nothing useful left to do -- there is no logger in this process and no console.
            // The service treats a missing answer file as `failed`, which is exactly right.
        }
        return 0;
    }

    /// <summary>Where the answer to a given request lives. One function so the writer and the
    /// reader cannot disagree.</summary>
    public static string AnswerPathFor(string requestPath) => requestPath + ".answer";

    private static string ShowOnDedicatedThread(MessageRequest request)
    {
        string outcome = MessageOutcomes.Failed;
        var thread = new Thread(() =>
        {
            try
            {
                InitialiseUi();
                using var dialog = new MessageDialog(request);
                dialog.ShowDialog();
                outcome = dialog.Outcome;
            }
            catch
            {
                // The styled dialog is the nice path, not the load-bearing one. If WinForms
                // cannot put a window up (an unusual desktop, a GDI+ failure), fall back to
                // the plain system prompt rather than reporting a failure for a message the
                // user would happily have answered. Same reasoning as ConsentBanner's
                // fallback -- but note it can only carry the OK/Cancel shape, so a
                // three-button message degrades to two.
                outcome = ShowFallback(request);
            }
        })
        {
            Name = "user-message",
            IsBackground = false,
        };
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();
        thread.Join();
        return outcome;
    }

    private static void InitialiseUi()
    {
        if (Interlocked.Exchange(ref _uiInitialised, 1) != 0) return;
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        try { Application.SetHighDpiMode(HighDpiMode.PerMonitorV2); } catch { }
    }

    /// <summary>Last-resort MessageBox. Maps onto whichever of the request's buttons it can
    /// actually offer: the first button is OK, a second is Cancel, and anything beyond that
    /// is unreachable -- so a user on this path can never accidentally trigger the third
    /// button's action by pressing one of the two shown.</summary>
    private static string ShowFallback(MessageRequest request)
    {
        var buttons = request.Buttons;
        string primary = buttons.Count > 0 ? buttons[0].Id : MessageOutcomes.Ok;
        string? secondary = buttons.Count > 1 ? buttons[1].Id : null;
        uint flags = (secondary is null ? MB_OK : MB_OKCANCEL)
                     | MB_ICONINFORMATION | MB_SYSTEMMODAL | MB_TOPMOST | MB_SETFOREGROUND;
        try
        {
            int result = MessageBoxTimeoutW(
                IntPtr.Zero, request.Body, request.Title, flags, 0,
                request.TimeoutSeconds > 0 ? (uint)request.TimeoutSeconds * 1000 : INFINITE);
            if (result == IDOK) return primary;
            if (result == IDCANCEL && secondary is not null) return secondary;
            if (result == MB_TIMEDOUT) return MessageOutcomes.Timeout;
            return MessageOutcomes.Dismissed;
        }
        catch
        {
            return MessageOutcomes.Failed;
        }
    }

    private const uint MB_OK = 0x00000000;
    private const uint MB_OKCANCEL = 0x00000001;
    private const uint MB_ICONINFORMATION = 0x00000040;
    private const uint MB_SYSTEMMODAL = 0x00001000;
    private const uint MB_TOPMOST = 0x00040000;
    private const uint MB_SETFOREGROUND = 0x00010000;
    private const int IDOK = 1;
    private const int IDCANCEL = 2;
    private const int MB_TIMEDOUT = 32000;
    private const uint INFINITE = 0xFFFFFFFF;

    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern int MessageBoxTimeoutW(
        IntPtr hWnd, string text, string caption, uint type, ushort languageId, uint milliseconds);
}
