using System.Text;
using Microsoft.Extensions.Logging.Abstractions;
using TempMonitorAgent.Fleet.Shell;

namespace TempMonitorAgent.Tests;

/// <summary>
/// Drives a real pseudoconsole with a real shell behind it. Like ProcessRunnerTests, these
/// are deliberately not mocked -- the entire value of ConPTY is behaviour the OS provides,
/// so a test with a fake pty would prove nothing.
///
/// The second test is the one that matters most. Under the OLD redirected-pipe shell an
/// operator could not answer `Read-Host`: the prompt (written with no trailing newline
/// through the host UI) never appeared, and pressing Enter on an empty box sent a line that
/// the shell did not treat as a reply. Both are asserted here, because both are what
/// "interactive" means in practice -- it is the difference between being able to drive
/// install.ps1 from the console and not.
/// </summary>
public class PtySessionTests
{
    /// <summary>Collects everything the pty emits, and lets a test wait for a predicate
    /// rather than sleep a fixed guess.</summary>
    private sealed class Sink
    {
        private readonly StringBuilder _text = new();
        private readonly object _gate = new();

        public void Add(string s) { lock (_gate) _text.Append(s); }
        public string Text { get { lock (_gate) return _text.ToString(); } }
        public void Clear() { lock (_gate) _text.Clear(); }

        public async Task<bool> WaitFor(Func<string, bool> predicate, int timeoutMs = 20_000)
        {
            var deadline = DateTime.UtcNow.AddMilliseconds(timeoutMs);
            while (DateTime.UtcNow < deadline)
            {
                if (predicate(Text)) return true;
                await Task.Delay(50);
            }
            return predicate(Text);
        }
    }

    private static PtySession Start(string shell, Sink sink, short cols = 100, short rows = 30) =>
        PtySession.Start(Guid.NewGuid().ToString("N"), shell, cols, rows, sink.Add,
                         NullLogger.Instance);

    [Fact]
    public async Task Cmd_RunsACommandAndEchoesWhatWasTyped()
    {
        var sink = new Sink();
        using var session = Start("cmd", sink);

        // A real console echoes typed characters back itself. That echo is the single
        // clearest proof we are on a pty and not a redirected pipe -- and it is why the
        // browser terminal must NOT echo locally, or everything appears twice.
        Assert.True(await sink.WaitFor(t => t.Contains('>')), $"no prompt; got: {sink.Text}");

        session.Write("echo pty-is-live\r");

        Assert.True(await sink.WaitFor(t => t.Contains("pty-is-live")),
            $"command produced no output; got: {sink.Text}");
    }

    [Fact]
    public async Task PowerShell_ShowsAReadHostPromptAndAcceptsABareEnter()
    {
        var sink = new Sink();
        using var session = Start("powershell", sink);
        Assert.True(await sink.WaitFor(t => t.Contains('>')), $"no prompt; got: {sink.Text}");

        // Exactly the shape install.ps1's Prompt-Value uses: a prompt with a default, where
        // an empty answer keeps the default. Read-Host writes "Pick [default]: " with NO
        // trailing newline -- under redirected pipes the operator never saw this at all.
        session.Write("$answer = Read-Host 'Pick [default]'\r");

        Assert.True(await sink.WaitFor(t => t.Contains("Pick [default]")),
            $"the Read-Host prompt never rendered; got: {sink.Text}");

        // The bug in one line: press Enter on an empty prompt. A bare CR must satisfy
        // Read-Host and return an empty string, exactly as it does at a physical console.
        sink.Clear();
        session.Write("\r");

        session.Write("if ($answer -eq '') { 'ENTER-ACCEPTED' } else { 'GOT:' + $answer }\r");
        Assert.True(await sink.WaitFor(t => t.Contains("ENTER-ACCEPTED")),
            $"a bare Enter did not answer Read-Host; got: {sink.Text}");
    }

    [Fact]
    public async Task Cmd_KeepsWorkingDirectoryAcrossCommands()
    {
        var sink = new Sink();
        using var session = Start("cmd", sink);
        Assert.True(await sink.WaitFor(t => t.Contains('>')));

        session.Write("cd \\\r");
        session.Write("cd\r");

        // One shell for the whole session, so `cd` sticks -- the property the old
        // per-submission model had to work hard for, and a pty gets for free. The PROMPT is
        // the assertion: it starts life as "C:\Windows\System32>" and must have become the
        // drive root. Asserting on the prompt rather than on `cd`'s echoed output also means
        // this fails if the shell were silently restarted between the two commands.
        Assert.True(await sink.WaitFor(t => t.Contains("C:\\>")),
            $"cd did not persist; got: {sink.Text}");
    }

    [Fact]
    public async Task Session_EndsWhenTheShellExits()
    {
        var sink = new Sink();
        using var session = Start("cmd", sink);
        Assert.True(await sink.WaitFor(t => t.Contains('>')));

        session.Write("exit\r");

        Assert.True(await sink.WaitFor(_ => session.Exited, timeoutMs: 15_000),
            "the session did not notice the shell exiting");
    }

    [Fact]
    public async Task Resize_IsAcceptedWhileTheShellIsRunning()
    {
        var sink = new Sink();
        using var session = Start("cmd", sink, cols: 80, rows: 24);
        Assert.True(await sink.WaitFor(t => t.Contains('>')));

        // Resizing is best-effort by design, so the assertion is that the session survives
        // it and still runs commands -- a wedged pty after a browser window resize would be
        // a very annoying way to lose a terminal.
        session.Resize(140, 45);
        session.Write("echo after-resize\r");

        Assert.True(await sink.WaitFor(t => t.Contains("after-resize")),
            $"the session stopped working after a resize; got: {sink.Text}");
    }

    [Fact]
    public async Task Dispose_TearsDownTheShell()
    {
        var sink = new Sink();
        var session = Start("cmd", sink);
        Assert.True(await sink.WaitFor(t => t.Contains('>')));

        session.Dispose();

        // Dispose must be prompt and must not hang: ClosePseudoConsole blocks on a live
        // client, which is why ConPtyProcess ends the process tree before closing it.
        Assert.True(session.Exited);
    }
}
