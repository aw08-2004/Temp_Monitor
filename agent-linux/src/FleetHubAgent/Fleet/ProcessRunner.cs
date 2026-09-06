using System.Diagnostics;
using System.Text;

namespace FleetHubAgent.Fleet;

/// <summary>What a child process did: its exit code and its combined output.</summary>
public readonly record struct ProcessRun(int ExitCode, string Output, bool TimedOut)
{
    public bool Succeeded => !TimedOut && ExitCode == 0;
}

/// <summary>
/// Runs one child process to completion and collects what it said.
///
/// **Arguments are passed as a LIST, never as a command line.** ProcessStartInfo.ArgumentList
/// hands the array straight to execve, so an operator-supplied hostname containing a space, a
/// quote or a semicolon is one argument and cannot become a second command. The string
/// Arguments property would re-parse it, and the shell that run_script deliberately invokes is
/// the only place a shell should be involved.
///
/// **stdout and stderr are read concurrently, not sequentially.** A child that fills the 64 KB
/// pipe buffer on the stream nobody is draining blocks forever, and the timeout below would
/// then be the only thing that ever ended it -- a bug that looks like "slow on big output" and
/// is really a deadlock. BeginOutputReadLine/BeginErrorReadLine drain both from the thread
/// pool.
///
/// They are also INTERLEAVED into one buffer, which is what an operator reading a console
/// transcript expects: a script that prints a step and then complains about it should show
/// those in that order, not all of stdout followed by all of stderr.
/// </summary>
public static class ProcessRunner
{
    /// <summary>
    /// Find a system program by name, trying the usual sbin/bin locations in order.
    ///
    /// **Not a hardcoded path, and not a bare name either.** /sbin/shutdown is right on Debian
    /// and Ubuntu; Arch merged /sbin into /usr/bin years ago, and a container image may have
    /// only one of them. Hardcoding one path makes `restart` fail on distros nobody tested,
    /// with an exit code that says "no such file" rather than anything about shutdown.
    ///
    /// Passing a bare name and letting execve search $PATH is the other obvious answer and is
    /// worse: this process runs as root from systemd, and resolving a program name through an
    /// inherited PATH is how a writable directory early in that PATH becomes root execution.
    /// The candidate list is fixed here, in the binary.
    ///
    /// Returns the first candidate that exists, or the first candidate unchanged when none do
    /// -- so the failure is "no such file: /sbin/shutdown", which names what was missing.
    /// </summary>
    public static string ProgramPath(params string[] candidates)
    {
        foreach (var candidate in candidates)
            if (File.Exists(candidate)) return candidate;
        return candidates[0];
    }

    /// <summary>Run a program with an argument list and no stdin.</summary>
    public static Task<ProcessRun> RunAsync(
        string fileName, IEnumerable<string> args, int timeoutSeconds,
        Action<string>? onOutput, CancellationToken ct) =>
        CoreAsync(fileName, args, stdin: null, timeoutSeconds, onOutput, ct);

    /// <summary>Run an interpreter and feed it <paramref name="stdin"/> as its program.
    ///
    /// This is how run_script hands a script over: argv is bounded by ARG_MAX and re-parsed
    /// by anything in the path, while stdin is neither. See RunScriptExecutor.</summary>
    public static Task<ProcessRun> RunStdinAsync(
        string fileName, string stdin, int timeoutSeconds,
        Action<string>? onOutput, CancellationToken ct) =>
        CoreAsync(fileName, Array.Empty<string>(), stdin, timeoutSeconds, onOutput, ct);

    private static async Task<ProcessRun> CoreAsync(
        string fileName, IEnumerable<string> args, string? stdin, int timeoutSeconds,
        Action<string>? onOutput, CancellationToken ct)
    {
        var psi = new ProcessStartInfo
        {
            FileName = fileName,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            RedirectStandardInput = true,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        foreach (var a in args) psi.ArgumentList.Add(a);

        using var proc = new Process { StartInfo = psi, EnableRaisingEvents = true };

        var buffer = new StringBuilder();
        var sink = new object();
        void Collect(string? line)
        {
            if (line is null) return;
            lock (sink) buffer.AppendLine(line);
            onOutput?.Invoke(line);
        }

        proc.OutputDataReceived += (_, e) => Collect(e.Data);
        proc.ErrorDataReceived += (_, e) => Collect(e.Data);

        proc.Start();
        proc.BeginOutputReadLine();
        proc.BeginErrorReadLine();

        // stdin is written and then CLOSED, always -- including when there is nothing to
        // write. A child left holding an open stdin it is waiting on (apt asking a
        // configuration question, a script with a stray `read`) would otherwise sit until the
        // timeout and be reported as a timeout rather than as what it is.
        try
        {
            if (stdin is not null) await proc.StandardInput.WriteAsync(stdin);
            proc.StandardInput.Close();
        }
        catch (IOException)
        {
            // The child exited before reading its input -- normal for a script whose first
            // line is `exit`. Not an error, and not worth failing the run over.
        }

        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
        timeout.CancelAfter(TimeSpan.FromSeconds(timeoutSeconds));

        try
        {
            await proc.WaitForExitAsync(timeout.Token);
            // The overload with no token also waits for the output handlers to drain, which
            // the token overload above does NOT do. Without it the last few lines of a chatty
            // command are lost in a race against the process ending.
            proc.WaitForExit();
        }
        catch (OperationCanceledException)
        {
            KillTree(proc);
            lock (sink)
                return new ProcessRun(-1,
                    buffer.ToString() + $"\n(killed after {timeoutSeconds}s)", TimedOut: true);
        }

        lock (sink) return new ProcessRun(proc.ExitCode, buffer.ToString(), TimedOut: false);
    }

    /// <summary>Kill the child AND anything it started.
    ///
    /// entireProcessTree matters more here than the name suggests: a timed-out shell that is
    /// killed alone leaves its children reparented to init and still running, holding the
    /// package lock or the file the next command needs. The operator sees a command that timed
    /// out and a machine that stays stuck.</summary>
    private static void KillTree(Process proc)
    {
        try { proc.Kill(entireProcessTree: true); }
        catch { /* already exited, or gone between the check and the call */ }
    }
}
