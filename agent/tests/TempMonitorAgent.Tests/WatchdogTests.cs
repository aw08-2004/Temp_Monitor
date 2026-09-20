using System.ServiceProcess;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging.Abstractions;
using TempMonitorAgent.Fleet;
using TempMonitorAgent.State;
using TempMonitorAgent.Watchdog;
using Xunit;

namespace TempMonitorAgent.Tests;

/// <summary>
/// Self-healing watchdogs (roadmap #20): the state machine that decides whether to restart a
/// service, and when to stop trying.
///
/// **Every failure this file guards is silent by construction**, which is what makes the file
/// worth its length. A watchdog doing its job produces nothing: no command, no output, no
/// console change. So does a watchdog that has quietly stopped working, one that gave up and
/// forgot to say so, and one whose flap counter resets itself. The console cannot tell those
/// apart and neither can an operator; these assertions are where they are supposed to be
/// caught.
///
/// Specifically:
///
///   * **A flap counter that resets.** If the count survives neither an agent restart nor a
///     document edit, a service that dies every ninety seconds is restarted forever and the
///     escalation -- the only part a human reads -- never arrives.
///   * **A flap limit that latches.** The opposite failure: a watchdog that stands down and
///     never tries again turns one bad afternoon into a machine that has silently stopped
///     protecting itself.
///   * **A grace period that does not hold.** Without it the watchdog fights every installer
///     on the machine, restarting a service the installer deliberately stopped -- which is the
///     one way this feature makes a machine worse rather than better.
///   * **A service coming up on its own being counted as down.** StartPending must not
///     accumulate toward the grace period, or the agent races the SCM.
///   * **A protected service accepted from the hub.** The hub refuses these too, and this is
///     the copy that actually protects the machine.
///   * **A malformed document being half-applied.** An entry with a missing bound must be
///     dropped, not defaulted to zero -- a zero grace is the installer-fighting case above.
/// </summary>
public class WatchdogTests
{
    // --- test doubles ------------------------------------------------------

    /// <summary>A service that is whatever the test says it is. `Restarts` is what the flap
    /// assertions count, and `StartOnRestart` is how a test says "this one will not come
    /// back".</summary>
    private sealed class FakeServices : IServiceControl
    {
        public ServiceControllerStatus? Status { get; set; } = ServiceControllerStatus.Running;
        public bool StartOnRestart { get; set; } = true;
        public int Restarts { get; private set; }

        public ServiceControllerStatus? StatusOf(string serviceName) => Status;

        public ServiceControl.Result Restart(string serviceName)
        {
            Restarts++;
            if (!StartOnRestart)
                return new ServiceControl.Result(false, false, "did not come back");
            Status = ServiceControllerStatus.Running;
            return new ServiceControl.Result(true, false, $"restarted the {serviceName} service");
        }
    }

    private static readonly DateTimeOffset T0 =
        new(2026, 9, 20, 12, 0, 0, TimeSpan.Zero);

    private static WatchdogEntry Entry(string service = "Spooler", int grace = 60,
                                       int maxRestarts = 3, int window = 3600, int id = 1) =>
        new()
        {
            Id = id,
            Service = service,
            GraceSeconds = grace,
            MaxRestarts = maxRestarts,
            WindowSeconds = window,
        };

    /// <summary>A fresh runner over a cleared state file.
    ///
    /// The clear is load-bearing. There is ONE watchdogs.json per state root and AssemblySetup
    /// gives the whole assembly one root, so without this every test here would inherit the
    /// previous test's flap history -- and the persistence test below would be the only one
    /// that noticed, by passing for the wrong reason. The reporter is drained for the same
    /// reason: it is static, and an undrained payload would be read as this test's.</summary>
    private static (WatchdogRunner runner, FakeServices services) Runner(
        params WatchdogEntry[] entries)
    {
        try { File.Delete(AgentConfig.WatchdogStatePath); } catch { /* never existed */ }
        WatchdogReporter.Invalidate();
        _ = WatchdogReporter.TakeIfChanged();
        var services = new FakeServices();
        var runner = new WatchdogRunner(NullLogger<WatchdogRunner>.Instance, new AgentState(),
                                        services);
        runner.Apply(new WatchdogDocument { Version = "v1", Entries = entries });
        _ = WatchdogReporter.TakeIfChanged();
        return (runner, services);
    }

    private static string? StatusReported()
    {
        var payload = WatchdogReporter.TakeIfChanged();
        return payload?["states"]?.AsArray().FirstOrDefault()?["status"]?.GetValue<string>();
    }

    // --- the document ------------------------------------------------------

    [Fact]
    public void A_heartbeat_with_no_document_is_not_an_empty_document()
    {
        // The distinction the whole release valve rests on. An ABSENT block means the hub had
        // nothing to say; an EMPTY list means "stop watching anything". Collapsing the two
        // would leave a machine enforcing a watchdog somebody deleted, with no way to stop it
        // short of uninstalling the agent.
        Assert.Null(WatchdogDocument.Parse(JsonNode.Parse("""{"other":1}"""), "v1"));
        var empty = WatchdogDocument.Parse(JsonNode.Parse("""{"watchdogs":[]}"""), "v1");
        Assert.NotNull(empty);
        Assert.Empty(empty!.Entries);
    }

    [Fact]
    public void An_entry_with_an_unusable_bound_is_dropped_not_defaulted()
    {
        var document = WatchdogDocument.Parse(JsonNode.Parse("""
        {"watchdogs":[
          {"id":1,"service":"Spooler","grace_seconds":60,"max_restarts":3,"window_seconds":3600},
          {"id":2,"service":"W32Time","max_restarts":3,"window_seconds":3600},
          {"id":3,"service":"","grace_seconds":60,"max_restarts":3,"window_seconds":3600},
          {"id":0,"service":"Themes","grace_seconds":60,"max_restarts":3,"window_seconds":3600},
          {"id":5,"service":"Themes","grace_seconds":60,"max_restarts":0,"window_seconds":3600}
        ]}
        """), "v1");

        Assert.NotNull(document);
        // Only the complete one survives. A missing grace defaulting to 0 is the case that
        // matters: it would make that watchdog fight every installer on the machine.
        Assert.Equal([1], document!.Entries.Select(e => e.Id));
    }

    // --- the state machine -------------------------------------------------

    [Fact]
    public void A_running_service_is_left_alone()
    {
        var (runner, services) = Runner(Entry());
        runner.Tick(T0);
        Assert.Equal(0, services.Restarts);
        Assert.Equal("ok", StatusReported());
    }

    [Fact]
    public void A_service_coming_up_on_its_own_is_not_counted_as_stopped()
    {
        var (runner, services) = Runner(Entry(grace: 0));
        services.Status = ServiceControllerStatus.StartPending;
        runner.Tick(T0);
        runner.Tick(T0.AddMinutes(5));
        // Even with no grace at all, the agent must not race the SCM into restarting a service
        // that is already starting.
        Assert.Equal(0, services.Restarts);
    }

    [Fact]
    public void The_grace_period_holds_before_anything_is_done()
    {
        var (runner, services) = Runner(Entry(grace: 60));
        services.Status = ServiceControllerStatus.Stopped;

        runner.Tick(T0);                      // first seen stopped
        runner.Tick(T0.AddSeconds(30));       // still inside the grace period
        Assert.Equal(0, services.Restarts);
        // And nothing is REPORTED either: an installer stopping a service for half a minute is
        // the ordinary case, and an event per occurrence would make the history unreadable.
        Assert.Null(StatusReported());

        runner.Tick(T0.AddSeconds(90));
        Assert.Equal(1, services.Restarts);
        Assert.Equal("restarted", StatusReported());
    }

    [Fact]
    public void The_grace_period_measures_this_outage_not_the_time_since_the_last_one()
    {
        var (runner, services) = Runner(Entry(grace: 60));
        services.Status = ServiceControllerStatus.Stopped;
        runner.Tick(T0);
        services.Status = ServiceControllerStatus.Running;
        runner.Tick(T0.AddSeconds(30));       // recovered on its own
        services.Status = ServiceControllerStatus.Stopped;
        runner.Tick(T0.AddSeconds(60));       // a NEW outage starts here

        runner.Tick(T0.AddSeconds(90));       // only 30s into it
        Assert.Equal(0, services.Restarts);
        runner.Tick(T0.AddSeconds(125));
        Assert.Equal(1, services.Restarts);
    }

    [Fact]
    public void The_flap_limit_stops_the_restarts_and_escalates()
    {
        var (runner, services) = Runner(Entry(grace: 0, maxRestarts: 3, window: 3600));
        var at = T0;
        for (var i = 0; i < 6; i++)
        {
            services.Status = ServiceControllerStatus.Stopped;
            at = at.AddSeconds(30);
            runner.Tick(at);
        }
        Assert.Equal(3, services.Restarts);
        Assert.Equal("given_up", StatusReported());
    }

    [Fact]
    public void The_flap_limit_clears_itself_once_the_window_passes()
    {
        // The counterpart to the test above, and the reason the limit is a count in a window
        // rather than a latch: a watchdog that stood down at lunchtime and never tried again
        // is a machine that has silently stopped protecting itself.
        var (runner, services) = Runner(Entry(grace: 0, maxRestarts: 2, window: 600));
        var at = T0;
        for (var i = 0; i < 4; i++)
        {
            services.Status = ServiceControllerStatus.Stopped;
            at = at.AddSeconds(30);
            runner.Tick(at);
        }
        Assert.Equal(2, services.Restarts);
        Assert.Equal("given_up", StatusReported());

        services.Status = ServiceControllerStatus.Stopped;
        runner.Tick(T0.AddSeconds(30 + 700));      // the first restart has aged out
        Assert.Equal(3, services.Restarts);
        Assert.Equal("restarted", StatusReported());
    }

    [Fact]
    public void A_restart_that_does_not_work_still_counts_toward_the_limit()
    {
        // Otherwise a service that refuses to start is retried every thirty seconds forever
        // and the escalation never arrives.
        var (runner, services) = Runner(Entry(grace: 0, maxRestarts: 2, window: 3600));
        services.StartOnRestart = false;
        services.Status = ServiceControllerStatus.Stopped;

        runner.Tick(T0.AddSeconds(30));
        Assert.Equal("failed", StatusReported());
        runner.Tick(T0.AddSeconds(60));
        runner.Tick(T0.AddSeconds(90));
        Assert.Equal(2, services.Restarts);
        Assert.Equal("given_up", StatusReported());
    }

    [Fact]
    public void A_service_that_is_not_installed_reads_as_missing_not_as_a_failure()
    {
        var (runner, services) = Runner(Entry(grace: 0));
        services.Status = null;
        runner.Tick(T0);
        Assert.Equal(0, services.Restarts);
        // `missing` is an authoring mistake the console shows and the hub does not alert on;
        // `failed` is a machine in trouble. Conflating them would raise an alert per PC for
        // one mistyped service name on a fleet-wide watchdog.
        Assert.Equal("missing", StatusReported());
    }

    [Fact]
    public void A_protected_service_is_refused_by_the_agent_too()
    {
        foreach (var name in new[] { "RpcSs", "rpcss", "WinMgmt", "TempMonitorAgent" })
        {
            var (runner, services) = Runner(Entry(service: name, grace: 0));
            services.Status = ServiceControllerStatus.Stopped;
            runner.Tick(T0.AddSeconds(30));
            Assert.Equal(0, services.Restarts);
            // Reported rather than silently skipped: the hub refuses these on save, so a hub
            // asking for one means the two lists have diverged and somebody has to know.
            Assert.Equal("failed", StatusReported());
        }
    }

    // --- persistence -------------------------------------------------------

    [Fact]
    public void The_flap_history_survives_an_agent_restart()
    {
        // The failure this exists for: a service that takes the agent down with it would
        // otherwise reset the counter on every crash, so the machine would restart it forever
        // and the hub would be told everything was fine.
        var entry = Entry(grace: 0, maxRestarts: 2, window: 3600);
        var (first, services) = Runner(entry);
        var at = T0;
        for (var i = 0; i < 2; i++)
        {
            services.Status = ServiceControllerStatus.Stopped;
            at = at.AddSeconds(30);
            first.Tick(at);
        }
        Assert.Equal(2, services.Restarts);

        // A brand-new runner over the same state directory: the agent restarting.
        var reborn = new FakeServices { Status = ServiceControllerStatus.Stopped };
        var second = new WatchdogRunner(NullLogger<WatchdogRunner>.Instance, new AgentState(),
                                        reborn);
        second.Tick(at.AddSeconds(30));
        Assert.Equal(0, reborn.Restarts);
        Assert.Equal("v1", second.Version);
    }

    [Fact]
    public void Editing_a_watchdog_does_not_clear_its_flap_history()
    {
        // Otherwise touching an unrelated field would be a way to reset the limit, which is
        // the sort of thing nobody discovers until the day it matters.
        var (runner, services) = Runner(Entry(grace: 0, maxRestarts: 2, window: 3600));
        var at = T0;
        for (var i = 0; i < 2; i++)
        {
            services.Status = ServiceControllerStatus.Stopped;
            at = at.AddSeconds(30);
            runner.Tick(at);
        }
        Assert.Equal(2, services.Restarts);

        // The edit changes the window, NOT the grace: a grace the restart could hide behind
        // would make this pass without the history having survived anything.
        runner.Apply(new WatchdogDocument
        {
            Version = "v2",
            Entries = [Entry(grace: 0, maxRestarts: 2, window: 7200)],
        });
        _ = WatchdogReporter.TakeIfChanged();
        services.Status = ServiceControllerStatus.Stopped;
        runner.Tick(at.AddSeconds(30));
        Assert.Equal(2, services.Restarts);
        Assert.Equal("given_up", StatusReported());
    }

    // --- the reporter ------------------------------------------------------

    [Fact]
    public void An_unchanged_state_is_not_re_reported_but_a_second_restart_is()
    {
        WatchdogReporter.Invalidate();
        _ = WatchdogReporter.TakeIfChanged();

        WatchdogReporter.Record(7, WatchdogStatus.Ok, 0, null, "");
        Assert.NotNull(WatchdogReporter.TakeIfChanged());
        WatchdogReporter.Record(7, WatchdogStatus.Ok, 0, null, "");
        Assert.Null(WatchdogReporter.TakeIfChanged());

        // Two restarts in a row are two restarts. Keying on the status word alone would
        // collapse them and lose exactly the evidence somebody goes looking for.
        WatchdogReporter.Record(7, WatchdogStatus.Restarted, 1, 1700, "");
        Assert.NotNull(WatchdogReporter.TakeIfChanged());
        WatchdogReporter.Record(7, WatchdogStatus.Restarted, 2, 1800, "");
        Assert.NotNull(WatchdogReporter.TakeIfChanged());
    }

    [Fact]
    public void The_wire_spelling_is_the_hubs_not_the_enums()
    {
        // hub/watchdogs.py matches on these exact strings, and ToString() would send
        // "GivenUp". A rename on either side has to be a visible break, not a silent one.
        Assert.Equal("given_up", WatchdogStatus.GivenUp.Wire());
        Assert.Equal("ok", WatchdogStatus.Ok.Wire());
        Assert.Equal("restarted", WatchdogStatus.Restarted.Wire());
        Assert.Equal("failed", WatchdogStatus.Failed.Wire());
        Assert.Equal("missing", WatchdogStatus.Missing.Wire());
    }
}
