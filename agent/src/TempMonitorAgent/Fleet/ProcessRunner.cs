using System.Diagnostics;
using System.Text;

namespace TempMonitorAgent.Fleet;

public readonly record struct ProcessOutcome(int ExitCode, string Output, bool TimedOut);

/// <summary>Runs a child process, capturing combined stdout/stderr with a timeout.
/// Used by the command executors (shutdown.exe, gpupdate, winget, scripts).</summary>
public static class ProcessRunner
{
    public static async Task<ProcessOutcome> RunAsync(
        string fileName, string arguments, CancellationToken ct,
        int timeoutSeconds = 300, string? workingDir = null)
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

        var sb = new StringBuilder();
        proc.OutputDataReceived += (_, e) => { if (e.Data is not null) sb.AppendLine(e.Data); };
        proc.ErrorDataReceived += (_, e) => { if (e.Data is not null) sb.AppendLine(e.Data); };

        proc.Start();
        proc.BeginOutputReadLine();
        proc.BeginErrorReadLine();

        using var timeoutCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        timeoutCts.CancelAfter(TimeSpan.FromSeconds(timeoutSeconds));

        try
        {
            await proc.WaitForExitAsync(timeoutCts.Token);
        }
        catch (OperationCanceledException)
        {
            try { proc.Kill(entireProcessTree: true); } catch { /* ignore */ }
            return new ProcessOutcome(-1, sb.ToString().Trim(), TimedOut: true);
        }

        return new ProcessOutcome(proc.ExitCode, sb.ToString().Trim(), TimedOut: false);
    }
}
