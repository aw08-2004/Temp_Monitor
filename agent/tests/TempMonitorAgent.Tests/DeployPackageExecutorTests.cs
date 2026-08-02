using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging.Abstractions;
using TempMonitorAgent.Fleet;
using TempMonitorAgent.Fleet.Executors;

namespace TempMonitorAgent.Tests;

/// <summary>
/// What a deploy's output actually looks like by the time an operator reads it.
///
/// This file exists because of a real defect: ProcessRunner hands its <c>onLine</c>
/// callback text that ALREADY ends in a newline, and this executor passed its
/// line-oriented <c>Say()</c> straight in — so every line of installer output was
/// double-spaced, in both the live console stream and the stored result log. Nothing
/// caught it, because the tests that existed asserted with Contains() and a doubled
/// newline still contains the text.
///
/// The deploy log is what someone reads at 2am when an install failed, so its shape is
/// worth pinning rather than eyeballing once.
/// </summary>
public class DeployPackageExecutorTests
{
    /// <summary>Never reached by these tests: a "command" package has no payload to
    /// fetch. Present because the executor takes the interface, which is exactly why it
    /// takes an interface.</summary>
    private sealed class UnusedDownloader : IPackageDownloader
    {
        public Task<string?> DownloadPackageAsync(string url, string destPath,
                                                  string? sha256, CancellationToken ct)
            => throw new InvalidOperationException("no payload should be fetched");
    }

    private static FleetCommand Command(string installArgs) => new()
    {
        Id = "cmd-1",
        Type = "deploy_package",
        Params = new JsonObject
        {
            ["package_name"] = "Test package",
            // "command" takes no payload, so the run reaches ProcessRunner directly.
            ["source"] = new JsonObject { ["kind"] = "command" },
            ["install_command"] = "cmd.exe",
            ["install_args"] = installArgs,
            ["success_exit_codes"] = new JsonArray(0),
            ["detection"] = new JsonObject { ["kind"] = "none" },
            ["timeout_seconds"] = 60,
        },
    };

    private static FleetCommand EchoCommand(string echo) => Command($"/c echo {echo}");

    private static DeployPackageExecutor NewExecutor() =>
        new(NullLogger<DeployPackageExecutor>.Instance, new UnusedDownloader());

    [Fact]
    public async Task InstallerOutputIsNotDoubleSpaced()
    {
        var streamed = new List<string>();
        var gate = new object();

        var result = await NewExecutor().ExecuteAsync(
            EchoCommand("hello"), t => { lock (gate) streamed.Add(t); },
            CancellationToken.None);

        var live = string.Concat(streamed);
        Assert.Contains("hello", live);
        // The actual assertion. A blank line after every line of installer output is what
        // the bug looked like, and it is invisible to a Contains() check.
        Assert.DoesNotContain("\n\n", live);
        Assert.DoesNotContain("\n\n", result.Output);
    }

    [Fact]
    public async Task TheStoredLogKeepsInstallerOutputEvenWithNobodyStreaming()
    {
        // onOutput is null when no live sink is attached. The installer's stdout still has
        // to reach the result, because command_results.output is the durable record of
        // what happened -- and it is the ONLY record, since this executor never reads
        // ProcessRunner's buffered Output. (Before the Say/Emit split, onLine was set to
        // null in this case and the installer's output was dropped entirely.)
        //
        // `ver` rather than `echo`: the executor logs the command line it is about to run,
        // so anything echoed would appear in the log via the ARGUMENTS whether or not the
        // process output ever reached it -- a test that passes for the wrong reason.
        var result = await NewExecutor().ExecuteAsync(
            Command("/c ver"), null, CancellationToken.None);

        Assert.Contains("Microsoft Windows", result.Output);
    }

    [Fact]
    public async Task AStepListRunsInsteadOfTheCommandLine()
    {
        // The wire shape a multi-step package arrives in: `steps` present, and the legacy
        // fields deliberately empty because the hub has no single-command projection for
        // it (see packages.build_command_params). The executor must take the steps.
        var cmd = new FleetCommand
        {
            Id = "cmd-2",
            Type = "deploy_package",
            Params = new JsonObject
            {
                ["package_name"] = "Stepped package",
                ["source"] = new JsonObject { ["kind"] = "multi", ["count"] = 0 },
                ["sources"] = new JsonArray(),
                ["steps"] = new JsonArray(
                    new JsonObject { ["kind"] = "run", ["command"] = "cmd.exe",
                                     ["args"] = "/c echo step-one" },
                    new JsonObject { ["kind"] = "run", ["command"] = "cmd.exe",
                                     ["args"] = "/c echo step-two" }),
                ["install_command"] = "",
                ["install_args"] = "",
                ["success_exit_codes"] = new JsonArray(0),
                ["detection"] = new JsonObject { ["kind"] = "none" },
                ["timeout_seconds"] = 60,
            },
        };

        var result = await NewExecutor().ExecuteAsync(cmd, null, CancellationToken.None);

        Assert.True(result.Success);
        Assert.Contains("step-one", result.Output);
        Assert.Contains("step-two", result.Output);
        // "no install command" is what an agent that predates steps says about these same
        // params. Seeing it here would mean the legacy projection won.
        Assert.DoesNotContain("no install command", result.Output);
    }

    [Fact]
    public async Task AFailedStepFailsTheDeployWithoutReachingDetection()
    {
        var cmd = new FleetCommand
        {
            Id = "cmd-3",
            Type = "deploy_package",
            Params = new JsonObject
            {
                ["package_name"] = "Doomed package",
                ["source"] = new JsonObject { ["kind"] = "multi" },
                ["steps"] = new JsonArray(
                    new JsonObject { ["kind"] = "run", ["command"] = "cmd.exe",
                                     ["args"] = "/c exit 5" }),
                ["install_command"] = "",
                ["success_exit_codes"] = new JsonArray(0),
                // A detection rule that WOULD pass. If the executor ran it anyway after a
                // failed step, this deploy would be reported as a success -- which is the
                // exact shape of "the installer did nothing and returned 0".
                ["detection"] = new JsonObject { ["kind"] = "none" },
                ["timeout_seconds"] = 60,
            },
        };

        var result = await NewExecutor().ExecuteAsync(cmd, null, CancellationToken.None);

        Assert.False(result.Success);
        Assert.Contains("FAILED at step 1", result.Output);
        Assert.DoesNotContain("[deploy] detection:", result.Output);
    }

    [Fact]
    public async Task OurOwnMessagesAreStillOnTheirOwnLines()
    {
        // The other half of the Say/Emit split: separating them must not run the
        // executor's own progress lines together into one unreadable paragraph.
        var result = await NewExecutor().ExecuteAsync(
            EchoCommand("hello"), null, CancellationToken.None);

        var lines = (result.Output ?? "").Split('\n', StringSplitOptions.RemoveEmptyEntries);
        Assert.Contains(lines, l => l.StartsWith("[deploy] Test package"));
        Assert.Contains(lines, l => l.StartsWith("[deploy] running:"));
        Assert.Contains(lines, l => l.Contains("exit code 0 accepted"));
    }
}
