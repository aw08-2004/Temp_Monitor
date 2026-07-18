using System.Diagnostics;
using System.Text;

namespace TempMonitorAgent.Fleet;

public readonly record struct ProcessOutcome(int ExitCode, string Output, bool TimedOut);

/// <summary>Runs a child process, capturing combined stdout/stderr with a timeout.
/// Used by the command executors (shutdown.exe, gpupdate, winget, scripts).
///
/// Pass <paramref name="onLine"/> to also receive each line as it arrives, which is how
/// a long-running command streams progress to the hub while it runs (see OutputStreamer).
/// The full text is still buffered and returned regardless, so the completion result is
/// unaffected by whether anyone is streaming.</summary>
public static class ProcessRunner
{
    public static async Task<ProcessOutcome> RunAsync(
        string fileName, string arguments, CancellationToken ct,
        int timeoutSeconds = 300, string? workingDir = null,
        Action<string>? onLine = null)
    {
        using var proc = new Process
        {
            StartInfo = new ProcessStartInfo
            {
                FileName = fileName,
                Arguments = arguments,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true,
                WorkingDirectory = workingDir ?? Environment.SystemDirectory,
            },
        };

        // OutputDataReceived and ErrorDataReceived are raised on separate threadpool
        // threads, so the buffer needs a lock (it never had one -- two interleaved
        // AppendLine calls could corrupt it). Invoking onLine inside the same lock also
        // hands the sink its lines already serialized, which is what lets OutputStreamer
        // assign sequence numbers without a second lock of its own.
        var sb = new StringBuilder();
        var gate = new object();
        void Handle(string? line)
        {
            if (line is null) return;
            lock (gate)
            {
                sb.AppendLine(line);
                // The sink (OutputStreamer.Add) now takes raw text, so re-add the newline the
                // line-event API stripped -- otherwise streamed lines would run together.
                try { onLine?.Invoke(line + "\n"); }
                catch { /* a broken sink must never kill the command */ }
            }
        }
        proc.OutputDataReceived += (_, e) => Handle(e.Data);
        proc.ErrorDataReceived += (_, e) => Handle(e.Data);

        proc.Start();
        proc.BeginOutputReadLine();
        proc.BeginErrorReadLine();

        using var timeoutCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        timeoutCts.CancelAfter(TimeSpan.FromSeconds(timeoutSeconds));

        string Snapshot() { lock (gate) { return sb.ToString().Trim(); } }

        try
        {
            await proc.WaitForExitAsync(timeoutCts.Token);
        }
        catch (OperationCanceledException)
        {
            try { proc.Kill(entireProcessTree: true); } catch { /* ignore */ }
            return new ProcessOutcome(-1, Snapshot(), TimedOut: true);
        }

        return new ProcessOutcome(proc.ExitCode, Snapshot(), TimedOut: false);
    }
}
