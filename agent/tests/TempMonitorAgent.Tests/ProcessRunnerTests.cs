using System.Diagnostics;
using TempMonitorAgent.Fleet;

namespace TempMonitorAgent.Tests;

/// <summary>
/// Drives real child processes. These are the only tests that prove the "live" in live
/// output: everything else could pass while output still arrived in one lump at the end.
/// </summary>
public class ProcessRunnerTests
{
    [Fact]
    public async Task OnLine_FiresWhileTheProcessIsStillRunning()
    {
        var seen = new List<(string Line, TimeSpan At)>();
        var gate = new object();
        var sw = Stopwatch.StartNew();

        var outcome = await ProcessRunner.RunAsync(
            "powershell.exe",
            "-NoProfile -NonInteractive -Command \"Write-Output 'first'; Start-Sleep -Seconds 2; Write-Output 'second'\"",
            CancellationToken.None, timeoutSeconds: 30,
            onLine: l => { lock (gate) seen.Add((l, sw.Elapsed)); });

        var total = sw.Elapsed;
        Assert.Contains(seen, s => s.Line.Contains("first"));
        Assert.Contains(seen, s => s.Line.Contains("second"));

        // The whole point: 'first' must reach the sink ~2s before the process exits. If
        // output were only surfaced at completion, this gap would be ~0.
        var firstAt = seen.First(s => s.Line.Contains("first")).At;
        Assert.True(total - firstAt > TimeSpan.FromSeconds(1),
            $"'first' surfaced at {firstAt.TotalSeconds:F2}s but the process ran until " +
            $"{total.TotalSeconds:F2}s — output was NOT streamed live.");
    }

    [Fact]
    public async Task OnLine_DeliversTextAlreadyTerminatedByExactlyOneNewline()
    {
        // The contract every onLine caller depends on, and which nothing pinned until a
        // real bug came of it: OutputStreamer.Add appends text VERBATIM, so ProcessRunner
        // re-adds the newline the line-event API strips. A caller that adds its own on top
        // double-spaces every line of output — which is precisely what DeployPackageExecutor
        // did by passing its line-oriented Say() straight in here.
        //
        // Asserted on the exact string rather than with Contains(), because Contains() is
        // what let the defect sit unnoticed in the tests that already existed.
        var streamed = new List<string>();
        var gate = new object();

        await ProcessRunner.RunAsync(
            "cmd.exe", "/c echo hello", CancellationToken.None,
            timeoutSeconds: 30, onLine: l => { lock (gate) streamed.Add(l); });

        Assert.Contains("hello\n", streamed);
        Assert.All(streamed, l => Assert.False(l.EndsWith("\n\n"),
            $"onLine delivered a double-terminated chunk: {l.Replace("\n", "\\n")}"));
    }

    [Fact]
    public async Task OnLine_DoesNotCannibalizeTheBufferedOutput()
    {
        var streamed = new List<string>();
        var gate = new object();

        var outcome = await ProcessRunner.RunAsync(
            "cmd.exe", "/c echo hello && echo world", CancellationToken.None,
            timeoutSeconds: 30, onLine: l => { lock (gate) streamed.Add(l); });

        // The result must stay complete whether or not anyone was streaming, because
        // command_results.output is the durable record.
        Assert.Contains("hello", outcome.Output);
        Assert.Contains("world", outcome.Output);
        Assert.Contains(streamed, l => l.Contains("hello"));
        Assert.Contains(streamed, l => l.Contains("world"));
        Assert.Equal(0, outcome.ExitCode);
    }

    [Fact]
    public async Task StderrAndStdout_BothReachTheSink()
    {
        var streamed = new List<string>();
        var gate = new object();

        await ProcessRunner.RunAsync(
            "powershell.exe",
            "-NoProfile -NonInteractive -Command \"Write-Output 'to-stdout'; [Console]::Error.WriteLine('to-stderr')\"",
            CancellationToken.None, timeoutSeconds: 30,
            onLine: l => { lock (gate) streamed.Add(l); });

        // They're merged deliberately -- the terminal renders them identically.
        Assert.Contains(streamed, l => l.Contains("to-stdout"));
        Assert.Contains(streamed, l => l.Contains("to-stderr"));
    }

    [Fact]
    public async Task NoSink_StillWorks()
    {
        // onLine is optional; restart/shutdown/rename pass null.
        var outcome = await ProcessRunner.RunAsync(
            "cmd.exe", "/c echo plain", CancellationToken.None, timeoutSeconds: 30);
        Assert.Contains("plain", outcome.Output);
        Assert.False(outcome.TimedOut);
    }

    [Fact]
    public async Task Timeout_KillsAndKeepsPartialOutput()
    {
        var outcome = await ProcessRunner.RunAsync(
            "powershell.exe",
            "-NoProfile -NonInteractive -Command \"Write-Output 'before-hang'; Start-Sleep -Seconds 30\"",
            CancellationToken.None, timeoutSeconds: 3);

        Assert.True(outcome.TimedOut);
        // A runaway script's output up to the kill is exactly what you need to debug it.
        Assert.Contains("before-hang", outcome.Output);
    }
}
