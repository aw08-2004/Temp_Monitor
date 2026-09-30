using FleetHubAgent.Fleet;
using FleetHubAgent.Telemetry;

namespace FleetHubAgent.Tests;

/// <summary>
/// The Linux Processes card (roadmap #22), and the ways a /proc walk goes wrong without
/// failing:
///
/// **comm is attacker-chosen text inside parentheses.** A process can name itself
/// `a) R 1 2 (`, and a reader that splits /proc/&lt;pid&gt;/stat on spaces shifts every field
/// after it -- the start time becomes a tick count and the CPU column a lie, with nothing
/// thrown anywhere.
///
/// **A reused pid must not inherit its predecessor's ticks.** Identity is (pid, starttime);
/// keyed on pid alone, a fresh process lands on a busy one's history and reports a delta that
/// is nonsense.
///
/// **The first sample is a baseline, not a list of zeros.** A card full of 0.0% on first open
/// reads as a machine doing nothing.
///
/// **The capability claim.** Without `processes` in the report the console's version gate
/// reads this agent's 0.x as too old and the card never fills -- the feature would exist and
/// be unreachable.
///
/// Unlike the Windows agent's reader these run here: the synthetic tree covers the parsing,
/// and one test walks this machine's own /proc.
/// </summary>
public class ProcessReaderTests : IDisposable
{
    private readonly string _root = Directory.CreateTempSubdirectory("fakeproc-").FullName;

    public void Dispose() => Directory.Delete(_root, recursive: true);

    private static string Stat(int pid, string comm, long utime, long stime, long start) =>
        // Fields 3..22 after the comm; 14/15 are utime/stime, 22 is starttime.
        $"{pid} ({comm}) S 1 1 1 0 -1 4194560 100 0 0 0 {utime} {stime} 0 0 20 0 1 0 {start} " +
        "1000 100 18446744073709551615 0 0 0 0 0 0 0 0 0 0 0 0 17 0 0 0 0 0 0";

    private void WriteMachine(long totalTicks)
    {
        File.WriteAllText(Path.Combine(_root, "stat"),
            $"cpu  {totalTicks} 0 0 0 0 0 0 0 0 0\ncpu0 1 2 3 4\nbtime 1700000000\n");
        File.WriteAllText(Path.Combine(_root, "meminfo"), "MemTotal:        8000000 kB\n");
    }

    private void WriteProcess(int pid, string comm, long ticks, long start, string cgroup = "")
    {
        var dir = Directory.CreateDirectory(Path.Combine(_root, pid.ToString())).FullName;
        File.WriteAllText(Path.Combine(dir, "stat"), Stat(pid, comm, ticks, 0, start));
        File.WriteAllText(Path.Combine(dir, "status"),
            $"Name:\t{comm}\nUid:\t0\t0\t0\t0\nVmRSS:\t   20480 kB\n");
        File.WriteAllText(Path.Combine(dir, "cgroup"), cgroup);
    }

    [Fact]
    public void A_hostile_comm_does_not_shift_the_fields()
    {
        var parsed = ProcessReader.ParseStat(Stat(42, "a) R 1 2 (", 300, 50, 9999));
        Assert.NotNull(parsed);
        Assert.Equal("a) R 1 2 (", parsed!.Value.Comm);
        Assert.Equal(300, parsed.Value.UserTicks);
        Assert.Equal(50, parsed.Value.SystemTicks);
        Assert.Equal(9999, parsed.Value.StartTicks);
    }

    [Theory]
    [InlineData("")]
    [InlineData("42 noparens S 1")]
    [InlineData("42 (short) S 1 2 3")]
    public void Garbage_stat_is_null_not_an_exception(string text) =>
        Assert.Null(ProcessReader.ParseStat(text));

    [Fact]
    public void Status_parses_name_uid_and_rss_and_a_kernel_thread_has_zero_rss()
    {
        var s = ProcessReader.ParseStatus("Name:\tsshd\nUid:\t1000\t1000\t1000\t1000\nVmRSS:\t  5120 kB\n");
        Assert.Equal(("sshd", (uint?)1000u, 5120L), s);
        Assert.Equal(0, ProcessReader.ParseStatus("Name:\tkworker/0:1\nUid:\t0\t0\t0\t0\n").RssKb);
    }

    [Fact]
    public void Machine_level_parsers()
    {
        Assert.Equal(60, ProcessReader.ParseCpuTotal("cpu  10 20 30\ncpu0 1 1 1\n"));
        Assert.Equal(1700000000, ProcessReader.ParseBootTime("cpu 1\nbtime 1700000000\n"));
        Assert.Equal(8000000, ProcessReader.ParseMemTotalKb("MemTotal:  8000000 kB\nMemFree: 1 kB\n"));
        Assert.Equal("root", ProcessReader.ParsePasswd("root:x:0:0::/root:/bin/bash\n")[0]);
    }

    [Theory]
    [InlineData("0::/system.slice/nginx.service\n", "nginx.service")]
    [InlineData("0::/user.slice/user-1000.slice/session-3.scope\n", null)]
    [InlineData("0::/system.slice/docker-abc.scope\n1:name=systemd:/system.slice/containerd.service\n", "containerd.service")]
    [InlineData("", null)]
    public void The_service_is_the_systemd_unit_and_only_a_service(string cgroup, string? expected) =>
        Assert.Equal(expected, ProcessReader.ParseUnit(cgroup));

    [Fact]
    public void The_first_sample_is_a_baseline_and_the_second_a_share_of_the_whole_machine()
    {
        WriteMachine(totalTicks: 1000);
        WriteProcess(10, "busy", ticks: 100, start: 500, cgroup: "0::/system.slice/busy.service\n");
        WriteProcess(11, "idle", ticks: 5, start: 600);
        var reader = new ProcessReader(_root);

        Assert.Null(reader.Sample());

        WriteMachine(totalTicks: 1200);                     // 200 ticks elapsed machine-wide
        WriteProcess(10, "busy", ticks: 150, start: 500);   // 50 of them: 25 %
        var snapshot = reader.Sample();

        Assert.NotNull(snapshot);
        var busy = snapshot!.Processes.Single(p => p.Pid == 10);
        Assert.Equal(25.0, busy.CpuPercent, 3);
        Assert.Equal(0.0, snapshot.Processes.Single(p => p.Pid == 11).CpuPercent, 3);
        Assert.Equal(10, snapshot.Processes[0].Pid);         // busiest first
        Assert.Equal(20.0, busy.MemoryMb, 3);
        Assert.Equal(1700000000 + 5, busy.StartedAt);        // btime + 500 ticks / 100 Hz
    }

    [Fact]
    public void A_reused_pid_starts_from_zero_not_from_its_predecessor()
    {
        WriteMachine(totalTicks: 1000);
        WriteProcess(10, "old", ticks: 900, start: 500);
        var reader = new ProcessReader(_root);
        reader.Sample();

        WriteMachine(totalTicks: 1200);
        WriteProcess(10, "new", ticks: 20, start: 7000);    // same pid, different process
        var snapshot = reader.Sample()!;

        // Keyed on pid alone this would be (20 - 900) / 200 -> clamped to 0 and hidden, or
        // worse on a different tick count. As a new identity it has no baseline, so 0.
        Assert.Equal(0.0, snapshot.Processes.Single().CpuPercent, 3);
    }

    [Fact]
    public void Reset_makes_the_next_sample_a_baseline_again()
    {
        WriteMachine(totalTicks: 1000);
        WriteProcess(10, "p", ticks: 1, start: 1);
        var reader = new ProcessReader(_root);
        reader.Sample();
        reader.Reset();
        Assert.Null(reader.Sample());
    }

    [Fact]
    public void This_machines_own_proc_walks_and_finds_this_process()
    {
        var reader = new ProcessReader();
        reader.Sample();
        Thread.Sleep(200);
        var snapshot = reader.Sample();
        Assert.NotNull(snapshot);
        Assert.Contains(snapshot!.Processes, p => p.Pid == Environment.ProcessId);
        Assert.True(snapshot.MemoryTotalMb > 0);
    }

    [Fact]
    public void Payload_carries_the_field_names_the_hub_reads()
    {
        var payload = ProcessReporter.ToPayload(new ProcessSnapshot(
            [new ProcessEntry(7, "nginx", 1.234, 12.34, "www-data", "/usr/sbin/nginx",
                              ["nginx.service"], 1700000000)], 4, 8000, 1000, 0));
        var item = payload["processes"]!.AsArray()[0]!;
        foreach (var field in new[] { "pid", "name", "cpu_pct", "mem_mb", "user", "path",
                                      "started_at", "services" })
            Assert.NotNull(item[field]);
        Assert.Equal(1.23, (double)item["cpu_pct"]!);
        Assert.Equal(0, (int)payload["truncated"]!);
    }

    [Fact]
    public void The_agent_claims_the_processes_feature()
    {
        // The hub's capabilities.FEATURE_PROCESSES; tests/test_capabilities.py pins the other
        // side. Without this claim the console's version gate hides the card.
        Assert.Contains("processes", AgentCapabilities.Implemented);
    }
}
