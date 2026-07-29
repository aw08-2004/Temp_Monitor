using System.Text;
using Microsoft.Extensions.Logging;

namespace TempMonitorAgent.Fleet.Shell;

/// <summary>
/// One live interactive terminal: a shell attached to a pseudoconsole (see
/// <see cref="ConPtyProcess"/>), with its VT output pumped to a callback and operator
/// keystrokes written back in.
///
/// This is a STREAM, not a sequence of commands. There is no submission framing, no sentinel,
/// no exit code and no cwd to report -- the operator is driving a terminal, and the terminal
/// ends when the shell does. That is the whole difference from <see cref="ShellSession"/>,
/// which still backs one-shot `run_script` (favorites, automation) where a discrete result
/// and exit code are the point.
///
/// What we deliberately do NOT do here:
///
///  * No interpreting the byte stream. Ctrl-C is 0x03 written to the pty, exactly as a
///    physical terminal would send it -- the console driver raises it as a real console
///    control event, so the child's own Ctrl-C handling runs. There is nothing to
///    special-case, and the old "kill the shell's children" approximation is gone.
///  * No line buffering, and no newline is ever added. A bare Enter is a single '\r' the
///    operator typed and must arrive as such; anything that "helpfully" trims or appends
///    breaks answering a prompt, which is the reason this exists.
///  * No echo. A real console echoes typed characters itself, so the terminal emulator must
///    NOT echo locally or every keystroke appears twice.
/// </summary>
public sealed class PtySession : IDisposable
{
    private readonly ConPtyProcess _pty;
    private readonly ILogger _log;
    private readonly FileStream _input;
    private readonly FileStream _output;
    private readonly Task _pump;
    private readonly Task _exitWatch;
    private readonly CancellationTokenSource _stop = new();
    private readonly object _writeGate = new();
    private volatile bool _disposed;

    /// <summary>Hub-side id this session streams under. Owned by the hub, not the agent.</summary>
    public string SessionId { get; }

    /// <summary>"powershell" or "cmd" -- what the operator asked for.</summary>
    public string Shell { get; }

    /// <summary>Set once the shell process has exited or the pty stream has ended.</summary>
    public bool Exited { get; private set; }

    /// <summary>Last time output or input moved, for the idle reaper.</summary>
    public DateTime LastActivityUtc { get; private set; } = DateTime.UtcNow;

    private PtySession(string sessionId, string shell, ConPtyProcess pty, Action<string> onOutput, ILogger log)
    {
        SessionId = sessionId;
        Shell = shell;
        _pty = pty;
        _log = log;
        // bufferSize: 1 disables FileStream's own buffering. On an interactive pty that is
        // not a micro-optimisation -- a buffered stream can hold a keystroke back waiting to
        // fill, and hold output back waiting for more, which is precisely the latency the
        // operator feels as "the terminal is laggy".
        _input = new FileStream(pty.InputWrite, FileAccess.Write, bufferSize: 1);
        _output = new FileStream(pty.OutputRead, FileAccess.Read, bufferSize: 1);
        _pump = Task.Run(() => PumpAsync(onOutput));
        _exitWatch = Task.Run(WatchForExitAsync);
    }

    /// <summary>Start a shell on a fresh pseudoconsole. <paramref name="onOutput"/> is called
    /// from the read pump with decoded VT text as it arrives -- it must not block or throw.</summary>
    public static PtySession Start(
        string sessionId, string shell, short cols, short rows, Action<string> onOutput, ILogger log)
    {
        var isCmd = shell is "cmd" or "batch" or "bat";
        // -NoLogo only: NO -NoProfile and NO "-Command -". This is an interactive login-style
        // shell, so the operator gets their profile and the normal REPL, prompt and all --
        // that prompt is now something a terminal emulator can render.
        var commandLine = isCmd
            ? "cmd.exe"
            : "powershell.exe -NoLogo -ExecutionPolicy Bypass";

        // Somewhere always-present to start; the operator cd's from there.
        var home = Environment.GetFolderPath(Environment.SpecialFolder.System);
        var pty = ConPtyProcess.Start(commandLine, home, cols, rows);
        log.LogInformation("Opened {Shell} pty session {Id} (pid {Pid})",
            isCmd ? "cmd" : "powershell", sessionId, pty.ProcessId);
        return new PtySession(sessionId, isCmd ? "cmd" : "powershell", pty, onOutput, log);
    }

    /// <summary>Write operator input to the console verbatim -- no trimming, no added newline.
    /// An empty string is a no-op; a lone "\r" is a perfectly valid Enter.</summary>
    public void Write(string data)
    {
        if (_disposed || string.IsNullOrEmpty(data)) return;
        var bytes = Encoding.UTF8.GetBytes(data);
        try
        {
            // The pump only reads, so the only contention is between concurrent input posts.
            lock (_writeGate)
            {
                _input.Write(bytes, 0, bytes.Length);
                _input.Flush();
            }
            LastActivityUtc = DateTime.UtcNow;
        }
        catch (Exception e)
        {
            _log.LogDebug("pty {Id}: write failed: {Msg}", SessionId, e.Message);
        }
    }

    /// <summary>Re-size the pseudoconsole to match the browser's terminal.</summary>
    public void Resize(short cols, short rows)
    {
        if (_disposed) return;
        _pty.Resize(cols, rows);
    }

    private async Task PumpAsync(Action<string> onOutput)
    {
        var buffer = new byte[8192];
        // A STATEFUL decoder: a UTF-8 sequence (or a VT escape carrying one) can straddle two
        // reads, and decoding each read independently would emit replacement characters at
        // every boundary. The decoder holds the partial sequence until the rest arrives.
        var decoder = Encoding.UTF8.GetDecoder();
        var chars = new char[buffer.Length + 8];

        while (!_stop.IsCancellationRequested)
        {
            int read;
            try { read = await _output.ReadAsync(buffer, 0, buffer.Length, _stop.Token); }
            catch (OperationCanceledException) { break; }
            catch (Exception e)
            {
                _log.LogDebug("pty {Id}: read ended: {Msg}", SessionId, e.Message);
                break;
            }
            if (read == 0) break;   // pipe EOF -- the pseudoconsole client is gone

            var n = decoder.GetChars(buffer, 0, read, chars, 0);
            if (n > 0)
            {
                LastActivityUtc = DateTime.UtcNow;
                try { onOutput(new string(chars, 0, n)); }
                catch (Exception e) { _log.LogDebug("pty {Id}: output sink threw: {Msg}", SessionId, e.Message); }
            }
        }

        Exited = true;
        _log.LogInformation("pty session {Id} output stream ended", SessionId);
    }

    /// <summary>
    /// Notice when the shell itself goes away (the operator typed `exit`, or it crashed).
    ///
    /// This has to watch the PROCESS, not the pipe, and that is a genuine difference from
    /// the redirected-pipe world. With plain redirection the output pipe EOFs the moment
    /// the child exits, so the read pump ending IS the child ending. Under a pseudoconsole
    /// the pty HOST holds the other end of that pipe and keeps it open after the client
    /// dies -- so the read pump would block forever and the session would look alive long
    /// after its shell was gone.
    /// </summary>
    private async Task WatchForExitAsync()
    {
        while (!_stop.IsCancellationRequested)
        {
            if (_pty.HasExited)
            {
                Exited = true;
                _log.LogInformation("pty session {Id} ended (exit code {Code})",
                    SessionId, _pty.ExitCode);
                return;
            }
            try { await Task.Delay(200, _stop.Token); }
            catch (OperationCanceledException) { return; }
        }
    }

    /// <summary>Wait for the shell to end (or the session to be torn down).</summary>
    public Task WaitForExitAsync(CancellationToken ct) => _exitWatch.WaitAsync(ct);

    /// <summary>Exit code of the shell once it has ended, or null while it is still running.</summary>
    public int? ExitCode => _pty.ExitCode;

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        Exited = true;
        _stop.Cancel();
        // Tear the pty down first: that closes the pipes, which is what unblocks the pump's
        // pending read. Waiting on the pump before disposing would hang.
        try { _pty.Dispose(); } catch { }
        try { _input.Dispose(); } catch { }
        try { _output.Dispose(); } catch { }
        try { Task.WaitAll(new[] { _pump, _exitWatch }, TimeSpan.FromSeconds(2)); } catch { }
        _stop.Dispose();
    }
}
