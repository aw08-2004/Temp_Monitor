using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging.Abstractions;
using FleetHubAgent.Fleet;
using FleetHubAgent.State;

namespace FleetHubAgent.Core.Tests;

/// <summary>
/// The app policy: what gets suspended, what can never be, and when it all lifts.
///
/// **Three of these assertions guard against bricking a device, and one guards against the
/// opposite.**
///
/// The never-suspend list is the first. Suspending the launcher, the settings app, the dialer
/// or this agent itself produces a phone that somebody has to factory reset -- and in the
/// agent's case, one that cannot even be told to stop, because the channel that would carry
/// the instruction is the thing that was suspended. hub/policy.py holds a copy so the console
/// never offers such a policy, but that copy is advice; this one is enforcement, and it has to
/// hold even if the hub is compromised or simply newer than this agent.
///
/// The dead-man switch is the second, and it points the other way: a device that stops hearing
/// from its hub lifts everything on its own. Without it, a phone whose hub was decommissioned
/// stays restricted forever. It is measured from the last CONFIRMATION rather than from first
/// receipt, so a policy the hub has been re-affirming every ten seconds for a week does not
/// expire -- getting that backwards would un-enforce a live fleet.
///
/// The third is that a malformed document is not read as an empty one. Empty means "lift
/// everything", which is a real instruction the hub sends deliberately; unparseable means the
/// hub said something this agent does not understand, and the safe answer to that is to keep
/// enforcing what is already applied.
///
/// And the fourth: an unmanaged device REPORTS that it cannot enforce rather than silently
/// doing nothing, because "not fully managed" and "policy applied" look identical in a console
/// that is only told about failures.
/// </summary>
public class DevicePolicyTests
{
    private static readonly DateTimeOffset T0 = new(2026, 9, 7, 12, 0, 0, TimeSpan.Zero);

    private sealed class FakeStore : IStateStore
    {
        private readonly Dictionary<string, string> _values = new(StringComparer.Ordinal);
        public bool Fail { get; set; }
        public string? Get(string key) => _values.TryGetValue(key, out var v) ? v : null;
        public bool Set(string key, string? value)
        {
            if (Fail) return false;
            if (value is null) _values.Remove(key); else _values[key] = value;
            return true;
        }
    }

    private sealed class FakeEnforcer(bool canEnforce = true) : IPolicyEnforcer
    {
        public bool CanEnforce { get; set; } = canEnforce;
        public List<IReadOnlyList<string>> Calls { get; } = [];
        public IReadOnlyList<string> Refuse { get; set; } = [];
        public Exception? Throw { get; set; }

        public IReadOnlyList<string> Apply(IReadOnlyList<string> packages)
        {
            if (Throw is not null) throw Throw;
            Calls.Add(packages);
            return Refuse;
        }
    }

    private static JsonObject Document(int maxAgeSeconds, params string[] blocked) => new()
    {
        ["blocked"] = new JsonArray(blocked.Select(p => (JsonNode)p!).ToArray()),
        ["max_age_seconds"] = maxAgeSeconds,
    };

    private static (PolicyCoordinator, FakeEnforcer, FakeStore) Build(bool managed = true)
    {
        var store = new FakeStore();
        var enforcer = new FakeEnforcer(managed);
        return (new PolicyCoordinator(NullLogger<PolicyCoordinator>.Instance,
                                      new AgentState(store), enforcer), enforcer, store);
    }

    // ---------------------------------------------------------------- the protected set

    [Theory]
    [InlineData("net.arkeanos.fleethub.agent")]
    [InlineData("com.android.settings")]
    [InlineData("com.sec.android.app.launcher")]
    [InlineData("com.google.android.apps.nexuslauncher")]
    [InlineData("com.android.dialer")]
    [InlineData("com.android.emergency")]
    [InlineData("com.android.systemui")]
    public void Nothing_can_suspend_a_protected_package(string package)
    {
        Assert.True(NeverSuspend.Contains(package));
    }

    [Fact]
    public void The_agent_protects_ITSELF_first()
    {
        // The one failure with no route back: suspending the agent ends management AND removes
        // the channel that could tell the device to stop.
        Assert.True(NeverSuspend.Contains("net.arkeanos.fleethub.agent"));
    }

    [Theory]
    [InlineData("com.zhiliaoapp.musically")]
    [InlineData("com.google.android.youtube")]
    [InlineData("")]
    public void An_ordinary_package_is_not_protected(string package)
    {
        Assert.False(NeverSuspend.Contains(package));
    }

    [Fact]
    public void A_protected_package_is_dropped_from_the_effective_list()
    {
        var policy = new DevicePolicy(
            "v1", ["com.zhiliaoapp.musically", "com.android.settings"],
            TimeSpan.FromDays(7), T0);
        Assert.Equal(["com.zhiliaoapp.musically"], policy.Effective(T0));
    }

    // ---------------------------------------------------------------- the dead-man switch

    [Fact]
    public void A_fresh_policy_is_enforced()
    {
        var policy = new DevicePolicy("v1", ["com.x"], TimeSpan.FromDays(7), T0);
        Assert.False(policy.IsExpired(T0.AddDays(6)));
        Assert.Equal(["com.x"], policy.Effective(T0.AddDays(6)));
    }

    [Fact]
    public void A_policy_the_hub_has_stopped_confirming_lifts_itself()
    {
        var policy = new DevicePolicy("v1", ["com.x"], TimeSpan.FromDays(7), T0);
        Assert.True(policy.IsExpired(T0.AddDays(8)));
        // The assertion that keeps a phone usable when its hub is gone.
        Assert.Empty(policy.Effective(T0.AddDays(8)));
    }

    [Fact]
    public void Confirmation_refreshes_the_clock_rather_than_being_a_no_op()
    {
        // The hub re-affirms the same version on every heartbeat. Anchoring on FIRST receipt
        // instead would expire a policy that has been confirmed every ten seconds for a week.
        var policy = new DevicePolicy("v1", ["com.x"], TimeSpan.FromDays(7), T0);
        policy.Reaffirm(T0.AddDays(6));
        Assert.False(policy.IsExpired(T0.AddDays(12)));
    }

    [Fact]
    public void An_absurd_max_age_is_clamped_at_both_ends()
    {
        // The FLOOR is the one that matters: a hub sending zero -- a bug, or a setting nobody
        // sanity-checked -- would make every device lift its policy on the next tick, which is
        // a fleet-wide un-enforcement with no operator anywhere in it.
        Assert.Equal(DevicePolicy.MinMaxAge,
            new DevicePolicy("v", [], TimeSpan.Zero, T0).MaxAge);
        Assert.Equal(DevicePolicy.MaxMaxAge,
            new DevicePolicy("v", [], TimeSpan.FromDays(3650), T0).MaxAge);
    }

    // ---------------------------------------------------------------- parsing

    [Fact]
    public void A_document_parses_into_a_policy()
    {
        var policy = DevicePolicy.Parse(Document(3600, "com.x", "com.y"), "v9", T0);
        Assert.NotNull(policy);
        Assert.Equal("v9", policy!.Version);
        Assert.Equal(["com.x", "com.y"], policy.Blocked);
        Assert.Equal(TimeSpan.FromHours(1), policy.MaxAge);
    }

    [Fact]
    public void An_EMPTY_document_is_a_real_instruction()
    {
        // "Lift everything." This is what the hub sends when a machine stops being covered by
        // any policy, and it is the release valve that makes removing a target work at all.
        var policy = DevicePolicy.Parse(Document(3600), "v10", T0);
        Assert.NotNull(policy);
        Assert.Empty(policy!.Blocked);
    }

    [Fact]
    public void A_MALFORMED_document_is_not_read_as_an_empty_one()
    {
        // The distinction the whole parse exists for. Empty is an instruction; unparseable
        // means the hub said something this agent does not understand.
        Assert.Null(DevicePolicy.Parse(null, "v", T0));
        Assert.Null(DevicePolicy.Parse(new JsonObject(), "v", T0));
        Assert.Null(DevicePolicy.Parse(new JsonObject { ["blocked"] = "com.x" }, "v", T0));
        Assert.Null(DevicePolicy.Parse(JsonValue.Create("nonsense"), "v", T0));
    }

    [Fact]
    public void A_document_with_no_max_age_falls_back_to_the_hubs_own_default()
    {
        var policy = DevicePolicy.Parse(
            new JsonObject { ["blocked"] = new JsonArray() }, "v", T0);
        Assert.Equal(DevicePolicy.DefaultMaxAge, policy!.MaxAge);
    }

    [Fact]
    public void A_policy_survives_a_round_trip_through_the_state_store()
    {
        // Persisted so a process kill resumes the STALENESS CLOCK, not merely the enforcement:
        // an agent that forgot it every restart would restart the clock with it, and a device
        // whose hub went silent a month ago would enforce forever, one kill at a time.
        var original = new DevicePolicy("v3", ["com.x"], TimeSpan.FromDays(2), T0);
        var restored = DevicePolicy.FromJson(JsonNode.Parse(original.ToJson().ToJsonString()));
        Assert.NotNull(restored);
        Assert.Equal("v3", restored!.Version);
        Assert.Equal(["com.x"], restored.Blocked);
        Assert.Equal(TimeSpan.FromDays(2), restored.MaxAge);
        Assert.Equal(T0, restored.ReceivedAtUtc);
    }

    // ---------------------------------------------------------------- the coordinator

    [Fact]
    public void Accepting_a_document_does_not_apply_it_yet()
    {
        // Applying is the inventory loop's job: suspending forty packages is a binder call
        // each on some builds, and the heartbeat decides whether the machine reads online.
        var (policy, enforcer, _) = Build();
        policy.Accept(Document(3600, "com.x"), "v1", T0);
        Assert.Empty(enforcer.Calls);
        Assert.Equal("v1", policy.CurrentVersion);
    }

    [Fact]
    public void A_tick_applies_the_held_policy()
    {
        var (policy, enforcer, _) = Build();
        policy.Accept(Document(3600, "com.x"), "v1", T0);
        policy.Tick(T0);
        Assert.Single(enforcer.Calls);
        Assert.Equal(["com.x"], enforcer.Calls[0]);
    }

    [Fact]
    public void A_second_tick_with_nothing_changed_does_nothing()
    {
        var (policy, enforcer, _) = Build();
        policy.Accept(Document(3600, "com.x"), "v1", T0);
        policy.Tick(T0);
        policy.Tick(T0.AddMinutes(1));
        Assert.Single(enforcer.Calls);
    }

    [Fact]
    public void A_new_version_is_applied()
    {
        var (policy, enforcer, _) = Build();
        policy.Accept(Document(3600, "com.x"), "v1", T0);
        policy.Tick(T0);
        policy.Accept(Document(3600, "com.x", "com.y"), "v2", T0.AddMinutes(1));
        policy.Tick(T0.AddMinutes(1));
        Assert.Equal(2, enforcer.Calls.Count);
        Assert.Equal(["com.x", "com.y"], enforcer.Calls[1]);
    }

    [Fact]
    public void The_dead_man_switch_lifts_everything_and_only_once()
    {
        var (policy, enforcer, _) = Build();
        policy.Accept(Document(3600, "com.x"), "v1", T0);
        policy.Tick(T0);

        policy.Tick(T0.AddHours(2));                   // an hour past the max age
        Assert.Equal(2, enforcer.Calls.Count);
        Assert.Empty(enforcer.Calls[1]);               // everything lifted
        policy.Tick(T0.AddHours(3));
        Assert.Equal(2, enforcer.Calls.Count);         // ...and not lifted again every tick
    }

    [Fact]
    public void A_protected_package_never_reaches_the_enforcer()
    {
        var (policy, enforcer, _) = Build();
        policy.Accept(Document(3600, "com.x", "com.android.settings"), "v1", T0);
        policy.Tick(T0);
        Assert.Equal(["com.x"], enforcer.Calls[0]);
    }

    [Fact]
    public void An_unmanaged_device_REPORTS_that_it_cannot_enforce()
    {
        // Silently doing nothing would look identical, in the console, to a policy that
        // applied -- which is the whole reason a device that cannot enforce says so.
        var (policy, enforcer, _) = Build(managed: false);
        policy.Accept(Document(3600, "com.x"), "v1", T0);
        policy.Tick(T0);

        Assert.Empty(enforcer.Calls);
        var state = policy.Read();
        Assert.NotNull(state);
        Assert.Contains("not fully managed", state!["error"]!.GetValue<string>());
        Assert.Equal(["com.x"],
            state["failed"]!.AsArray().Select(n => n!.GetValue<string>()));
    }

    [Fact]
    public void Refused_packages_reach_the_report()
    {
        // The field the whole report exists for: a policy applied to two packages that took on
        // one is a compliance answer that is confidently wrong unless this survives.
        var (policy, enforcer, _) = Build();
        enforcer.Refuse = ["com.y"];
        policy.Accept(Document(3600, "com.x", "com.y"), "v1", T0);
        policy.Tick(T0);

        var state = policy.Read();
        Assert.Equal(["com.y"],
            state!["failed"]!.AsArray().Select(n => n!.GetValue<string>()));
        Assert.Equal("v1", state["version"]!.GetValue<string>());
    }

    [Fact]
    public void An_enforcer_that_throws_is_recorded_rather_than_taking_down_the_loop()
    {
        var (policy, enforcer, _) = Build();
        enforcer.Throw = new InvalidOperationException("binder died");
        policy.Accept(Document(3600, "com.x"), "v1", T0);
        policy.Tick(T0);

        Assert.Contains("binder died", policy.Read()!["error"]!.GetValue<string>());
    }

    [Fact]
    public void An_enforcer_that_threw_is_retried_on_the_next_tick()
    {
        // The version is NOT recorded as applied when the attempt threw, so the next tick
        // tries again rather than believing a policy landed that did not.
        var (policy, enforcer, _) = Build();
        enforcer.Throw = new InvalidOperationException("binder died");
        policy.Accept(Document(3600, "com.x"), "v1", T0);
        policy.Tick(T0);
        enforcer.Throw = null;
        policy.Tick(T0.AddMinutes(1));
        Assert.Single(enforcer.Calls);
    }

    [Fact]
    public void Nothing_is_reported_before_anything_has_been_applied()
    {
        // A device that has never had a policy sends no block at all, rather than an empty
        // report the console would have to special-case.
        var (policy, _, _) = Build();
        Assert.Null(policy.Read());
    }

    [Fact]
    public void A_restored_policy_keeps_its_original_confirmation_time()
    {
        var store = new FakeStore();
        var enforcer = new FakeEnforcer();
        var first = new PolicyCoordinator(NullLogger<PolicyCoordinator>.Instance,
                                          new AgentState(store), enforcer);
        first.Accept(Document(3600, "com.x"), "v1", T0);

        var second = new PolicyCoordinator(NullLogger<PolicyCoordinator>.Instance,
                                           new AgentState(store), enforcer);
        second.Restore();
        Assert.Equal("v1", second.CurrentVersion);
        // Two hours after the ORIGINAL receipt, with a one-hour max age: a restart must not
        // have reset the clock.
        second.Tick(T0.AddHours(2));
        Assert.Empty(enforcer.Calls[^1]);
    }

    [Fact]
    public void A_store_that_cannot_be_written_still_leaves_the_policy_applied()
    {
        // Persistence buys the staleness clock surviving a restart, not the enforcement
        // itself -- so a failed write is a log line, never a policy that does not apply.
        var store = new FakeStore { Fail = true };
        var enforcer = new FakeEnforcer();
        var policy = new PolicyCoordinator(NullLogger<PolicyCoordinator>.Instance,
                                           new AgentState(store), enforcer);
        policy.Accept(Document(3600, "com.x"), "v1", T0);
        policy.Tick(T0);
        Assert.Single(enforcer.Calls);
    }

    [Fact]
    public void The_report_is_a_change_only_block_like_every_other_inventory()
    {
        var (policy, _, _) = Build();
        Assert.Equal("policy_state", policy.Key);
    }
}
