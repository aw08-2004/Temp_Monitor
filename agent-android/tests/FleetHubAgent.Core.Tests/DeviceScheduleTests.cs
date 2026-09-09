using System.Text.Json.Nodes;
using FleetHubAgent.Fleet;

namespace FleetHubAgent.Core.Tests;

/// <summary>
/// Curfews and daily budgets, evaluated on the device -- roadmap #23 phase E.
///
/// **The wrapped window is what this file exists for.** A bedtime rule is 22:00 to 07:00, which
/// is the only kind of window an operator actually writes, and it is the one shape a naive
/// "start, then now, then end" comparison gets wrong. Getting it wrong lifts the curfew at
/// midnight -- exactly when it matters and exactly when nobody is looking at the console to
/// notice. Worse, the morning half belongs to the day AFTER the one the window names, so a
/// Sunday-night curfew that checks today's index at 02:00 on Monday reads as inactive.
///
/// The second silent failure is the whole-device rule expanding to nothing, or to everything.
/// "*" means the device, and expanding it needs an inventory the agent may not have yet. No
/// inventory must expand to NOTHING: the other way round, a freshly enrolled phone suspends
/// every app it owns on the first tick after its inventory lands, at 22:00, with no operator in
/// the loop.
///
/// The third is a protected package reached without anybody naming it. A per-app curfew cannot
/// name the launcher, because the console refuses -- but a whole-device curfew does not name
/// anything, and the expansion is where the launcher gets in.
///
/// And the fourth: one malformed rule must not take the schedule with it. Half a curfew is worth
/// having; a policy that parsed to Empty because one budget had a string where a number belonged
/// is an un-enforcement nobody asked for.
/// </summary>
public class DeviceScheduleTests
{
    // 2026-09-07 is a MONDAY, so day index 0. Every time below is local.
    private static DateTimeOffset Local(int day, int hour, int minute = 0) =>
        new(2026, 9, day, hour, minute, 0, TimeSpan.Zero);

    private static readonly IReadOnlyDictionary<string, int> NoUsage =
        new Dictionary<string, int>();

    private static DeviceSchedule Schedule(JsonObject document) => DeviceSchedule.Parse(document);

    private static JsonObject Window(int[] days, int start, int end, params string[] packages)
        => new()
        {
            ["days"] = new JsonArray(days.Select(d => (JsonNode)d).ToArray()),
            ["start"] = start,
            ["end"] = end,
            ["packages"] = new JsonArray(packages.Select(p => (JsonNode)p!).ToArray()),
        };

    private static JsonObject Budget(string package, int minutes)
        => new() { ["package"] = package, ["minutes"] = minutes };

    private static JsonObject Document(JsonArray? windows = null, JsonArray? budgets = null)
        => new() { ["windows"] = windows ?? [], ["budgets"] = budgets ?? [] };

    // ---------------------------------------------------------------- windows within a day

    [Fact]
    public void A_window_is_active_inside_its_hours_and_only_on_its_days()
    {
        // 09:00-17:00, Monday and Tuesday only.
        var schedule = Schedule(Document([Window([0, 1], 9 * 60, 17 * 60, "com.x")]));

        Assert.Equal(["com.x"], schedule.Blocked(Local(7, 12), NoUsage, []));   // Monday noon
        Assert.Empty(schedule.Blocked(Local(7, 8, 59), NoUsage, []));           // Monday, before
        Assert.Empty(schedule.Blocked(Local(7, 17), NoUsage, []));              // end exclusive
        Assert.Empty(schedule.Blocked(Local(9, 12), NoUsage, []));              // Wednesday
    }

    // ---------------------------------------------------------------- the wrapped window

    [Fact]
    public void A_bedtime_window_holds_across_midnight()
    {
        // 22:00-07:00 on Monday. The evening half is Monday's; the morning half is Tuesday's.
        var schedule = Schedule(Document([Window([0], 22 * 60, 7 * 60, "com.x")]));

        Assert.Equal(["com.x"], schedule.Blocked(Local(7, 22, 30), NoUsage, []));  // Mon 22:30
        // The assertion the whole file is about: the curfew does NOT lift at midnight.
        Assert.Equal(["com.x"], schedule.Blocked(Local(8, 0, 30), NoUsage, []));   // Tue 00:30
        Assert.Equal(["com.x"], schedule.Blocked(Local(8, 6, 59), NoUsage, []));   // Tue 06:59
        Assert.Empty(schedule.Blocked(Local(8, 7), NoUsage, []));                  // Tue 07:00
        // Tuesday EVENING is not covered -- the window names Monday, and this is the trap on the
        // other side: reading the morning half as "any day" would curfew every night of the week.
        Assert.Empty(schedule.Blocked(Local(8, 22, 30), NoUsage, []));
    }

    [Fact]
    public void The_morning_half_of_a_Sunday_window_lands_on_Monday()
    {
        // Sunday is index 6, and the day after it wraps back to 0. Reading "yesterday" without
        // the wrap would look at Saturday on a Monday morning and answer no.
        var schedule = Schedule(Document([Window([6], 22 * 60, 7 * 60, "com.x")]));
        Assert.Equal(["com.x"], schedule.Blocked(Local(13, 23), NoUsage, []));   // Sun 23:00
        Assert.Equal(["com.x"], schedule.Blocked(Local(14, 6), NoUsage, []));    // Mon 06:00
    }

    // ---------------------------------------------------------------- budgets

    [Fact]
    public void A_per_app_budget_bites_when_it_is_spent_and_not_before()
    {
        var schedule = Schedule(Document(budgets: [Budget("com.x", 60)]));
        var under = new Dictionary<string, int> { ["com.x"] = 59 * 60 };
        var spent = new Dictionary<string, int> { ["com.x"] = 60 * 60 };

        Assert.Empty(schedule.Blocked(Local(7, 12), under, []));
        Assert.Equal(["com.x"], schedule.Blocked(Local(7, 12), spent, []));
    }

    [Fact]
    public void A_device_budget_counts_every_app_including_ones_no_rule_mentions()
    {
        // "Two hours of screen time" means the screen. Summing only the packages a policy names
        // would give a budget that never runs out on a device whose apps nobody listed.
        var schedule = Schedule(Document(budgets: [Budget("*", 60)]));
        var mixed = new Dictionary<string, int> { ["com.x"] = 40 * 60, ["com.y"] = 25 * 60 };

        Assert.Equal(["com.x", "com.y"],
            schedule.Blocked(Local(7, 12), mixed, ["com.x", "com.y"]));
    }

    [Fact]
    public void A_zero_budget_is_a_blocklist_and_not_a_missing_value()
    {
        // Zero minutes means "not at all today", which is a real thing to write. Treating it as
        // unset -- the obvious null-ish reading -- silently un-enforces the strictest rule there
        // is.
        var schedule = Schedule(Document(budgets: [Budget("com.x", 0)]));
        Assert.Equal(["com.x"], schedule.Blocked(Local(7, 12), NoUsage, []));
    }

    // ---------------------------------------------------------------- expanding the device rule

    [Fact]
    public void A_whole_device_curfew_with_no_inventory_blocks_nothing()
    {
        var schedule = Schedule(Document([Window([0], 22 * 60, 7 * 60, "*")]));
        Assert.Empty(schedule.Blocked(Local(7, 23), NoUsage, []));
    }

    [Fact]
    public void A_whole_device_curfew_expands_to_the_inventory()
    {
        var schedule = Schedule(Document([Window([0], 22 * 60, 7 * 60, "*")]));
        Assert.Equal(["com.x", "com.y"],
            schedule.Blocked(Local(7, 23), NoUsage, ["com.y", "com.x"]));
    }

    [Fact]
    public void The_expansion_cannot_reach_a_protected_package()
    {
        // The one path where a rule reaches the launcher without anybody having named it.
        var schedule = Schedule(Document([Window([0], 22 * 60, 7 * 60, "*")]));
        var installed = new[]
        {
            "com.x", "com.sec.android.app.launcher", "com.android.settings",
            "net.arkeanos.fleethub.agent",
        };
        Assert.Equal(["com.x"], schedule.Blocked(Local(7, 23), NoUsage, installed));
    }

    // ---------------------------------------------------------------- both kinds together

    [Fact]
    public void A_window_and_a_budget_both_hold_with_nothing_reconciled()
    {
        var schedule = Schedule(Document(
            [Window([0], 22 * 60, 7 * 60, "com.x")],
            [Budget("com.y", 30)]));
        var spent = new Dictionary<string, int> { ["com.y"] = 45 * 60 };

        Assert.Equal(["com.x", "com.y"], schedule.Blocked(Local(7, 23), spent, []));
        // Outside the window, the budget still bites on its own.
        Assert.Equal(["com.y"], schedule.Blocked(Local(7, 12), spent, []));
    }

    // ---------------------------------------------------------------- parsing

    [Fact]
    public void A_malformed_rule_is_dropped_without_taking_the_schedule_with_it()
    {
        var windows = new JsonArray
        {
            Window([0], 9 * 60, 17 * 60, "com.good"),
            new JsonObject { ["days"] = new JsonArray(0), ["start"] = "nine" },  // unparseable
            Window([], 9 * 60, 17 * 60, "com.nodays"),                           // no days
            Window([0], 600, 600, "com.equal"),                                  // ambiguous
            Window([0], 9 * 60, 5000, "com.outofrange"),                         // past midnight
        };
        var schedule = Schedule(Document(windows, [Budget("com.x", -5), Budget("com.y", 30)]));

        Assert.Single(schedule.Windows);
        Assert.Single(schedule.Budgets);
        Assert.Equal(["com.good"], schedule.Blocked(Local(7, 12), NoUsage, []));
    }

    [Fact]
    public void A_window_with_no_packages_covers_the_whole_device()
    {
        // An operator who writes "nothing between 22:00 and 07:00" and lists no apps means the
        // device. Reading it as "no apps" would give a rule that does nothing at all.
        var schedule = Schedule(Document([Window([0], 22 * 60, 7 * 60)]));
        Assert.Equal([DeviceSchedule.EveryPackage], schedule.Windows[0].Packages);
    }

    [Theory]
    [InlineData("null")]
    [InlineData("[]")]
    [InlineData("123")]
    [InlineData("{}")]
    public void Anything_that_is_not_a_schedule_parses_to_the_empty_one(string json)
    {
        var schedule = DeviceSchedule.Parse(JsonNode.Parse(json));
        Assert.True(schedule.IsEmpty);
    }

    // ---------------------------------------------------------------- through DevicePolicy

    [Fact]
    public void A_policy_carries_its_schedule_through_a_round_trip()
    {
        var document = new JsonObject
        {
            ["blocked"] = new JsonArray("com.blocked"),
            ["max_age_seconds"] = 3600,
            ["schedule"] = Document([Window([0], 22 * 60, 7 * 60, "com.curfew")],
                                    [Budget("com.budget", 0)]),
        };
        var policy = DevicePolicy.Parse(document, "v1", Local(7, 23));
        Assert.NotNull(policy);

        var restored = DevicePolicy.FromJson(JsonNode.Parse(policy!.ToJson().ToJsonString()));
        Assert.NotNull(restored);
        Assert.Single(restored!.Schedule.Windows);
        Assert.Single(restored.Schedule.Budgets);
    }

    private sealed class FakeUsage(DateTimeOffset now, Dictionary<string, int>? seconds = null)
        : IUsageSource
    {
        private readonly Dictionary<string, int> _seconds = seconds ?? [];
        public bool CanRead => true;
        public DateTimeOffset LocalNow { get; } = now;
        public IReadOnlyDictionary<string, int> SecondsToday() => _seconds;
    }

    [Fact]
    public void The_schedule_adds_to_the_blocked_list_rather_than_replacing_it()
    {
        var policy = new DevicePolicy("v1", ["com.blocked"],
            Schedule(Document([Window([0], 22 * 60, 7 * 60, "com.curfew")])),
            TimeSpan.FromDays(7), Local(7, 12));

        Assert.Equal(["com.blocked"],
            policy.Effective(Local(7, 12), new FakeUsage(Local(7, 12)), []));
        Assert.Equal(["com.blocked", "com.curfew"],
            policy.Effective(Local(7, 23), new FakeUsage(Local(7, 23)), []));
    }

    [Fact]
    public void An_expired_policy_lifts_its_curfew_too()
    {
        // The dead-man switch has to reach the schedule half as well. A phone whose hub is gone
        // must not keep a bedtime forever.
        var policy = new DevicePolicy("v1", [],
            Schedule(Document([Window([0], 22 * 60, 7 * 60, "com.curfew")])),
            TimeSpan.FromHours(1), Local(7, 12));

        Assert.Empty(policy.Effective(Local(14, 23), new FakeUsage(Local(14, 23)), []));
    }
}
