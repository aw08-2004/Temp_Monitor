using System.Diagnostics;
using System.Text;
using Microsoft.Extensions.Logging;

namespace TempMonitorAgent.Fleet.Shell;

/// <summary>Outcome of one submission run in a persistent shell.</summary>
public readonly record struct SubmissionOutcome(int ExitCode, string? Cwd, bool TimedOut, bool ShellDied);

/// <summary>
/// One long-lived interactive shell (powershell.exe or cmd.exe) driven over redirected
/// stdin, so `cd`, environment, and variables persist between submissions -- a real
/// terminal on the box, not a fresh process per command. One of these is held per
/// (operator, shell) by <see cref="ShellSessionManager"/>.
///
/// The framing below was validated empirically against Windows PowerShell 5.1 / cmd.exe;
/// each rule earned its place:
///
///   PowerShell — launched as a stdin REPL (`-Command -`). A submission is sent as ONE
///     physical line so the REPL never waits for `>>` continuation:
///       try { . ([ScriptBlock]::Create(<utf8-from-base64>)) 2>&1 } catch { $_ }
///       finally { <sentinel to stderr>; <sentinel + $LASTEXITCODE + $PWD to stdout> }
///     - DOT-SOURCE (.), not call (&): & runs in a child scope, losing `$x = 1` between
///       submissions; dot-sourcing runs in the session scope so variables persist.
///     - 2>&1 wraps the INVOCATION (not `iex … 2>&1`, which drops errors) to merge stderr.
///     - try/finally so a terminating error (throw / -ErrorAction Stop) can't skip the
///       sentinel and hang the submission.
///     - No `??` (that's PowerShell 7+); coalesce $LASTEXITCODE with an `if`.
///
///   cmd.exe — launched `/Q /K` (NO /V:ON: it would eat literal `!` in user scripts). Each
///     submission is a temp .cmd invoked with `call`, so `cd`/`set` persist to the shell;
///     the user body runs as a `call :__user` subroutine so its `exit /b N` returns to the
///     wrapper and the sentinel (last thing executed) still fires.
///
///   Both — a dual-stream sentinel (a per-submission GUID written to BOTH stdout and
///     stderr) closes the stdout/stderr ordering race: the stdout marker also carries exit
///     code + cwd, and completion means the marker was seen on both drained pipes. See
///     <see cref="SubmissionParser"/>.
/// </summary>
public sealed class ShellSession : IAsyncDisposable
{
    private readonly string _shell;   // "powershell" | "cmd"
    private readonly ILogger _log;
    private readonly Process _proc;
    private readonly Task _readOut;
    private readonly Task _readErr;
    private readonly string _tempDir;
    private readonly List<string> _tempFiles = new();

    // Only one submission runs at a time; shell_input bypasses this to reach a blocked child.
    private readonly SemaphoreSlim _runLock = new(1, 1);
    private readonly object _stateGate = new();
    private SubmissionParser? _active;
    private TaskCompletionSource<bool>? _activeDone;

    private volatile bool _shellExited;

    public string Shell => _shell;

    /// <summary>False once the underlying shell process has exited (operator ran `exit`, a
    /// crash, or disposal). The manager recreates a dead session on next use.</summary>
    public bool IsAlive => !_shellExited;

    /// <summary>True while a submission is executing (used by the manager's idle reaper and
    /// the Worker's fast-poll decision).</summary>
    public bool HasActiveSubmission { get { lock (_stateGate) return _active is not null; } }

    /// <summary>Last time a submission finished, for idle-timeout reaping.</summary>
    public DateTime LastActivityUtc { get; private set; } = DateTime.UtcNow;

    private ShellSession(string shell, Process proc, ILogger log, string tempDir)
    {
        _shell = shell;
        _proc = proc;
        _log = log;
        _tempDir = tempDir;
        _readOut = Task.Run(() => PumpAsync(_proc.StandardOutput, isStdout: true));
        _readErr = Task.Run(() => PumpAsync(_proc.StandardError, isStdout: false));
        _proc.EnableRaisingEvents = true;
        _proc.Exited += (_, _) => _shellExited = true;
    }

    /// <summary>Launch a shell and prime it (encoding + suppressed prompt), then drain the
    /// banner/first prompt so the next real submission starts clean.</summary>
    public static async Task<ShellSession> StartAsync(string shell, ILogger log, CancellationToken ct)
    {
        var isCmd = shell is "cmd" or "batch" or "bat";
        var tempDir = Path.Combine(Path.GetTempPath(), "tmagent_shell", Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(tempDir);

        var psi = new ProcessStartInfo
        {
            FileName = isCmd ? "cmd.exe" : "powershell.exe",
            Arguments = isCmd ? "/Q /K" : "-NoProfile -NoLogo -ExecutionPolicy Bypass -Command -",
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
            StandardOutputEncoding = new UTF8Encoding(false),
            StandardErrorEncoding = new UTF8Encoding(false),
            // Start somewhere sane and always-present; the operator cd's from here.
            WorkingDirectory = Environment.GetFolderPath(Environment.SpecialFolder.System),
        };

        var proc = Process.Start(psi) ?? throw new InvalidOperationException($"could not start {psi.FileName}");
        proc.StandardInput.AutoFlush = true;
        try { ProcessTree.AssignToKillOnCloseJob(proc.Handle); } catch { }

        var session = new ShellSession(isCmd ? "cmd" : "powershell", proc, log, tempDir);

        // Preamble: UTF-8 output + no prompt noise.
        await session._proc.StandardInput.WriteLineAsync(isCmd
            ? "chcp 65001 >nul & prompt $S"
            : "[Console]::OutputEncoding=[Text.Encoding]::UTF8; function prompt { '' }");

        // Sync: run a no-op submission and discard its output, so a later real submission
        // doesn't inherit the shell's start-up chatter.
        await session.RunAsync(isCmd ? "rem sync" : "$null", timeoutSeconds: 20, _ => { }, ct);
        return session;
    }

    /// <summary>Run one submission to completion, streaming output via <paramref name="onOutput"/>.
    /// Serialized: a second submission waits for the first. On timeout the shell's children are
    /// killed but the shell is kept.</summary>
    public async Task<SubmissionOutcome> RunAsync(
        string script, int timeoutSeconds, Action<string> onOutput, CancellationToken ct)
    {
        if (_shellExited) return new SubmissionOutcome(-1, null, TimedOut: false, ShellDied: true);

        await _runLock.WaitAsync(ct);
        try
        {
            var marker = "TMDONE_" + Guid.NewGuid().ToString("N");
            var parser = new SubmissionParser(marker, onOutput);
            var done = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
            lock (_stateGate) { _active = parser; _activeDone = done; }

            try
            {
                await WriteSubmissionAsync(script, marker, ct);
            }
            catch (Exception e)
            {
                lock (_stateGate) { _active = null; _activeDone = null; }
                _log.LogWarning(e, "Failed to write submission to {Shell} shell", _shell);
                return new SubmissionOutcome(-1, null, TimedOut: false, ShellDied: _shellExited);
            }

            var timeout = Task.Delay(TimeSpan.FromSeconds(Math.Max(1, timeoutSeconds)), ct);
            var finished = await Task.WhenAny(done.Task, timeout);

            bool timedOut = false;
            if (finished != done.Task)
            {
                // The submission overran. First try to kill just what it SPAWNED (a hung
                // .\app.exe, a `ping -t`); that's the common case and keeps the shell with its
                // cwd/variables, after which the shell's finally/sentinel runs and the parser
                // completes right below.
                timedOut = true;
                _log.LogInformation("Submission timed out after {N}s; killing shell children", timeoutSeconds);
                ProcessTree.KillDescendants(_proc.Id);
                var grace = await Task.WhenAny(done.Task, Task.Delay(TimeSpan.FromSeconds(10), ct));
                if (grace != done.Task)
                {
                    // Nothing to kill, or it didn't help -- the hang is IN the shell itself (an
                    // in-process cmdlet like Start-Sleep, an infinite loop). The only way to
                    // stop it is to take the shell down; the session is lost, so recycle it.
                    onOutput("\n[agent] submission would not stop; recycling the shell (session reset).\n");
                    try { if (!_proc.HasExited) _proc.Kill(entireProcessTree: true); } catch { }
                    await Task.WhenAny(done.Task, Task.Delay(TimeSpan.FromSeconds(3), ct));
                }
            }

            var cwd = parser.Cwd;
            var exit = parser.ExitCode ?? -1;
            var shellDied = _shellExited && !parser.Complete;
            if (shellDied)
            {
                var residual = parser.DrainResidual();
                if (residual.Length > 0) onOutput(residual);
            }

            lock (_stateGate) { _active = null; _activeDone = null; }
            LastActivityUtc = DateTime.UtcNow;
            return new SubmissionOutcome(exit, cwd, timedOut, shellDied);
        }
        finally
        {
            _runLock.Release();
        }
    }

    /// <summary>Write text straight to the shell's stdin -- reaches a program the current
    /// submission is blocked reading (answering a Y/N prompt). Deliberately does NOT take the
    /// run lock: it must land while a submission holds it.</summary>
    public void WriteInput(string text)
    {
        if (_shellExited) return;
        try
        {
            // The console sends a line; ensure exactly one trailing newline so the child's
            // ReadLine unblocks without doubling blank lines.
            _proc.StandardInput.Write(text.TrimEnd('\r', '\n'));
            _proc.StandardInput.Write('\n');
            _proc.StandardInput.Flush();
        }
        catch (Exception e)
        {
            _log.LogDebug("WriteInput to {Shell} shell failed: {Msg}", _shell, e.Message);
        }
    }

    /// <summary>Ctrl-C equivalent: kill the shell's children, keep the shell.</summary>
    public void SignalInterrupt() => ProcessTree.KillDescendants(_proc.Id);

    private async Task WriteSubmissionAsync(string script, string marker, CancellationToken ct)
    {
        if (_shell == "cmd")
        {
            var cmdPath = Path.Combine(_tempDir, Guid.NewGuid().ToString("N") + ".cmd");
            var body =
                "@echo off\r\n" +
                "goto :__main\r\n" +
                ":__user\r\n" + script + "\r\n" +
                "goto :eof\r\n" +
                ":__main\r\n" +
                "call :__user\r\n" +
                "set \"__tmec=%ERRORLEVEL%\"\r\n" +
                ">&2 echo " + marker + "\r\n" +
                "echo " + marker + " %__tmec% %CD%\r\n";
            await File.WriteAllTextAsync(cmdPath, body, new UTF8Encoding(false), ct);
            CleanupOldTempFiles();
            lock (_tempFiles) _tempFiles.Add(cmdPath);
            await _proc.StandardInput.WriteLineAsync($"call \"{cmdPath}\"");
        }
        else
        {
            var b64 = Convert.ToBase64String(Encoding.UTF8.GetBytes(script));
            // Built by concatenation (not $"") so every { } below is literal PowerShell.
            var line =
                "try { . ([ScriptBlock]::Create([Text.Encoding]::UTF8.GetString(" +
                "[Convert]::FromBase64String('" + b64 + "')))) 2>&1 } catch { $_ } finally { " +
                "$ec=$global:LASTEXITCODE; if($null -eq $ec){$ec=0}; " +
                "[Console]::Error.WriteLine('" + marker + "'); " +
                "[Console]::Out.WriteLine(\"" + marker + " $ec $($PWD.ProviderPath)\") }";
            await _proc.StandardInput.WriteLineAsync(line);
        }
    }

    private async Task PumpAsync(StreamReader reader, bool isStdout)
    {
        var chars = new char[8192];
        while (true)
        {
            int n;
            try { n = await reader.ReadAsync(chars, 0, chars.Length); }
            catch { break; }
            if (n == 0) break;   // stream closed -> shell exited
            var fragment = new string(chars, 0, n);

            SubmissionParser? parser;
            TaskCompletionSource<bool>? done;
            lock (_stateGate) { parser = _active; done = _activeDone; }
            if (parser is null) continue;   // between submissions; the shell should be silent

            if (isStdout) parser.FeedStdout(fragment); else parser.FeedStderr(fragment);
            if (parser.Complete) done?.TrySetResult(true);
        }

        // Stream ended: the shell died. Unblock any waiter.
        _shellExited = true;
        lock (_stateGate) _activeDone?.TrySetResult(true);
    }

    // Keep only a couple of recent temp .cmd files: cmd reads them lazily, so we can't delete
    // the one currently running, but we needn't keep every past submission's file around.
    private void CleanupOldTempFiles()
    {
        List<string> toDelete;
        lock (_tempFiles)
        {
            if (_tempFiles.Count <= 3) return;
            toDelete = _tempFiles.GetRange(0, _tempFiles.Count - 3);
            _tempFiles.RemoveRange(0, _tempFiles.Count - 3);
        }
        foreach (var f in toDelete) { try { File.Delete(f); } catch { } }
    }

    public async ValueTask DisposeAsync()
    {
        try { _proc.StandardInput.Close(); } catch { }
        try { if (!_proc.HasExited) _proc.Kill(entireProcessTree: true); } catch { }
        await Task.WhenAny(Task.WhenAll(_readOut, _readErr), Task.Delay(2000));
        try { _proc.Dispose(); } catch { }
        _runLock.Dispose();
        try { Directory.Delete(_tempDir, recursive: true); } catch { }
    }
}
