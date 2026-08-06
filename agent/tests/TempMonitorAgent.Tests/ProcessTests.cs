using System.Text.Json.Nodes;
using TempMonitorAgent.Fleet;
using TempMonitorAgent.Fleet.Executors;
using TempMonitorAgent.Telemetry;
using Xunit;

namespace TempMonitorAgent.Tests;

/// <summary>
/// The two halves of the Processes card that can be tested off a live machine: the guard
/// that decides what may be ended, and the wire payload the hub parses.
///
/// **The guard is the one that matters.** Ending csrss or lsass does not close a program, it
/// bugchecks the box, and the pid an operator clicked is always a few seconds old on a system
/// that recycles ids within minutes. Both refusals are pure functions over a name, so both
/// are asserted here rather than being discovered on somebody's PC.
///
/// **The wire payload is a contract with Python.** `tests/test_processes.py` asserts the same
/// field names from the other side; drift between them is not a crash but a Processes card
/// that quietly shows nothing.
///
/// **What this cannot cover** is <c>ProcessReader.Sample()</c> against a real machine: the
/// CPU deltas, the token reads that fail for protected processes, and the WMI service map.
/// Those need processes to exist and to be doing something, which a build agent cannot
/// promise -- so what is tested here is everything that does not.
/// </summary>
public class ProcessTests
{
    // ------------------------------------------------------------------ the guard

    [Theory]
    [InlineData("lsass.exe")]
    [InlineData("LSASS")]
    [InlineData("  csrss.EXE  ")]
    [InlineData("wininit.exe")]
    [InlineData("winlogon")]
    [InlineData("services.exe")]
    [InlineData("smss.exe")]
    [InlineData("System")]
    [InlineData("Registry")]
    [InlineData("Memory Compression")]
    // A service host takes every service inside it down with it; for the RPC host that
    // means the machine restarts itself in a minute. restart_process is the answer there.
    [InlineData("svchost.exe")]
    // Ending the agent loses the machine from the console AND loses this command's own
    // result, so the operator would watch a spinner for work that did happen.
    [InlineData("TempMonitorAgent.exe")]
    public void CriticalProcessesAreRefusedHoweverTheyAreSpelled(string name)
    {
        Assert.True(ProcessGuard.IsProtected(name));
    }

    [Theory]
    [InlineData("chrome.exe")]
    [InlineData("OUTLOOK.EXE")]
    [InlineData("spoolsv.exe")]
    // Deliberately killable: restarting Explorer is a real helpdesk action and Windows
    // brings it straight back.
    [InlineData("explorer.exe")]
    [InlineData("")]
    public void OrdinaryProgramsAreNot(string name)
    {
        Assert.False(ProcessGuard.IsProtected(name));
    }

    [Theory]
    [InlineData("chrome.exe", "chrome")]
    [InlineData("CHROME", "chrome.exe")]
    [InlineData(" chrome.EXE ", "Chrome")]
    public void TheNameFromTheListAndTheNameOnTheMachineAgreeAcrossSpelling(
        string fromList, string onMachine)
    {
        Assert.True(ProcessGuard.NameMatches(fromList, onMachine));
    }

    [Theory]
    // The whole point: a pid recycled between the snapshot and the click must end nothing.
    [InlineData("chrome.exe", "lsass.exe")]
    [InlineData("chrome", "chromedriver")]
    [InlineData("chrome", "")]
    [InlineData("", "chrome")]
    [InlineData("", "")]
    public void AMismatchedOrMissingNameNeverMatches(string fromList, string onMachine)
    {
        Assert.False(ProcessGuard.NameMatches(fromList, onMachine));
    }

    // ------------------------------------------------------------------ the wire payload

    private static ProcessSnapshot Snapshot(params ProcessEntry[] entries) => new()
    {
        Processes = entries,
        CpuCores = 8,
        MemoryTotalMb = 16384,
        SampleMillis = 5000,
        Truncated = 3,
    };

    [Fact]
    public void ThePayloadCarriesTheFieldsTheHubStores()
    {
        var payload = ProcessReporter.ToPayload(Snapshot(new ProcessEntry
        {
            Pid = 4812,
            Name = "chrome",
            CpuPercent = 42.567,
            MemoryMb = 512.44,
            User = @"CORP\alice",
            Session = 1,
            Path = @"C:\Program Files\Google\Chrome\chrome.exe",
            StartedAt = 1754000000,
            Services = new[] { "Spooler" },
        }));

        Assert.Equal(8, payload["cpu_cores"]!.GetValue<int>());
        Assert.Equal(5000, payload["sample_ms"]!.GetValue<int>());
        Assert.Equal(3, payload["truncated"]!.GetValue<int>());

        var entry = payload["processes"]!.AsArray()[0]!;
        Assert.Equal(4812, entry["pid"]!.GetValue<int>());
        Assert.Equal("chrome", entry["name"]!.GetValue<string>());
        // Rounded on the way out: the console renders one decimal, and shipping fifteen
        // digits of a number measured over a five-second window is noise with a cost.
        Assert.Equal(42.57, entry["cpu_pct"]!.GetValue<double>(), 2);
        Assert.Equal(512.4, entry["mem_mb"]!.GetValue<double>(), 1);
        Assert.Equal(@"CORP\alice", entry["user"]!.GetValue<string>());
        Assert.Equal(1, entry["session"]!.GetValue<int>());
        Assert.Equal(1754000000, entry["started_at"]!.GetValue<long>());
        Assert.Equal("Spooler", entry["services"]!.AsArray()[0]!.GetValue<string>());
    }

    [Fact]
    public void AnUnknownStartTimeIsNullRatherThanNineteenSeventy()
    {
        var payload = ProcessReporter.ToPayload(Snapshot(new ProcessEntry
        {
            Pid = 4, Name = "System", StartedAt = null,
        }));

        var entry = payload["processes"]!.AsArray()[0]!;
        // The hub stores "unknown" distinctly. A 0 here would be rendered as a fact -- a
        // process that started at the epoch.
        Assert.Null(entry["started_at"]);
        // ...and a process hosting nothing carries no services key at all, rather than an
        // empty array that reads like an answer.
        Assert.Null(entry["services"]);
    }

    [Fact]
    public void AnEmptySnapshotIsStillAWellFormedPayload()
    {
        var payload = ProcessReporter.ToPayload(Snapshot());

        Assert.Empty(payload["processes"]!.AsArray());
        Assert.True(payload.ContainsKey("captured_at"));
    }

    // ------------------------------------------------------------------ the watch

    [Fact]
    public void NothingIsPendingUntilSomebodyIsWatching()
    {
        ProcessReporter.SetWanted(false);

        Assert.False(ProcessReporter.Wanted);
        Assert.False(ProcessReporter.HasPending);
        // The heartbeat asks every ten seconds whatever else is happening; an unwatched
        // machine must contribute nothing to that body.
        Assert.Null(ProcessReporter.TakeLatest());
    }

    [Fact]
    public void TurningTheWatchOffDropsAnythingStillWaitingToBeSent()
    {
        ProcessReporter.SetWanted(true);
        Assert.True(ProcessReporter.Wanted);

        ProcessReporter.SetWanted(false);
        // A sample taken for an operator who has since closed the card is not worth sending,
        // and sending it would re-populate a hub row nobody is reading.
        Assert.Null(ProcessReporter.TakeLatest());
        Assert.False(ProcessReporter.Wanted);
    }

    // ------------------------------------------------------------------ command params

    private static FleetCommand Command(string type, JsonObject parameters) =>
        new() { Id = "cmd-1", Type = type, Params = parameters };

    [Fact]
    public async Task KillRefusesACommandThatNamesNoProcess()
    {
        var executor = new KillProcessExecutor(
            Microsoft.Extensions.Logging.Abstractions.NullLogger<KillProcessExecutor>.Instance);

        var result = await executor.ExecuteAsync(
            Command("kill_process", new JsonObject { ["pids"] = new JsonArray(4812) }),
            null, CancellationToken.None);

        // The name is not decoration: without it the pid is an instruction to kill whatever
        // holds that id right now.
        Assert.False(result.Success);
        Assert.Contains("params.name", result.Output);
    }

    [Fact]
    public async Task KillRefusesACommandWithNoPids()
    {
        var executor = new KillProcessExecutor(
            Microsoft.Extensions.Logging.Abstractions.NullLogger<KillProcessExecutor>.Instance);

        var result = await executor.ExecuteAsync(
            Command("kill_process", new JsonObject { ["name"] = "chrome" }),
            null, CancellationToken.None);

        Assert.False(result.Success);
        Assert.Contains("params.pids", result.Output);
    }

    [Fact]
    public async Task RestartRefusesACommandMissingEitherHalfOfThePairing()
    {
        var executor = new RestartProcessExecutor(
            Microsoft.Extensions.Logging.Abstractions.NullLogger<RestartProcessExecutor>.Instance);

        var result = await executor.ExecuteAsync(
            Command("restart_process", new JsonObject { ["name"] = "chrome" }),
            null, CancellationToken.None);

        Assert.False(result.Success);
        Assert.Contains("params.pid", result.Output);
    }

    [Fact]
    public async Task RestartSaysSoWhenTheProcessHasAlreadyGone()
    {
        var executor = new RestartProcessExecutor(
            Microsoft.Extensions.Logging.Abstractions.NullLogger<RestartProcessExecutor>.Instance);

        // A pid that cannot exist: the list an operator clicked is always a little stale, and
        // "already gone" has to read as an outcome rather than as a fault.
        var result = await executor.ExecuteAsync(
            Command("restart_process",
                    new JsonObject { ["name"] = "chrome", ["pid"] = 0x7FFFFFFE }),
            null, CancellationToken.None);

        Assert.False(result.Success);
        Assert.Contains("no longer running", result.Output);
    }
}
