using System.Text.Json.Nodes;

namespace FleetHubAgent.Fleet;

/// <summary>
/// Curfews and daily budgets -- roadmap #23 phase E.
///
/// **Evaluated on the device, and that is not an optimisation.** A curfew depends on what time
/// it is where the device is; a budget depends on how much the device has been used today. The
/// hub has neither fact promptly, and a phone at 22:00 on a train with no signal still has a
/// bedtime. So the hub sends the RULES and this evaluates them every minute, whether or not
/// anything is reachable.
///
/// **The device's own local clock, with no timezone from the hub.** A curfew is about the
/// evening of the person holding the phone: a hub-imposed zone would put a traveller's curfew at
/// the wrong hour, and a fleet across two zones would need a policy each. The device owner
/// enforces automatic time so the clock is not the person's to move (see DeviceOwner), which is
/// what makes trusting the local clock defensible rather than naive.
///
/// **Windows and budgets both apply; nothing is reconciled.** If one rule says "no TikTok after
/// 22:00" and another says "sixty minutes a day", both hold and whichever bites first is the
/// answer. Combining them would mean inventing a precedence nobody asked for, and the strictest
/// reading is what an operator expects from a restriction.
/// </summary>
public sealed class DeviceSchedule
{
    /// <summary>A window or budget naming this package means the DEVICE rather than one app.
    /// Matches hub/policy.py's EVERY_PACKAGE.</summary>
    public const string EveryPackage = "*";

    private const int MinutesInDay = 24 * 60;

    public IReadOnlyList<TimeWindow> Windows { get; }
    public IReadOnlyList<UsageBudget> Budgets { get; }

    public DeviceSchedule(IReadOnlyList<TimeWindow> windows, IReadOnlyList<UsageBudget> budgets)
    {
        Windows = windows;
        Budgets = budgets;
    }

    public static DeviceSchedule Empty { get; } = new([], []);

    public bool IsEmpty => Windows.Count == 0 && Budgets.Count == 0;

    /// <summary>What this schedule says to suspend right now.
    ///
    /// <paramref name="localNow"/> is the device's own local time.
    /// <paramref name="secondsToday"/> is foreground seconds per package for the local day.
    /// <paramref name="installed"/> is needed only to expand <see cref="EveryPackage"/>, which
    /// is why a device whose inventory has not arrived yet still gets its per-app rules.
    /// </summary>
    public IReadOnlyList<string> Blocked(
        DateTimeOffset localNow,
        IReadOnlyDictionary<string, int> secondsToday,
        IReadOnlyList<string> installed)
    {
        var blocked = new HashSet<string>(StringComparer.Ordinal);

        foreach (var window in Windows)
        {
            if (!window.IsActive(localNow)) continue;
            foreach (var package in Expand(window.Packages, installed)) blocked.Add(package);
        }

        foreach (var budget in Budgets)
        {
            var spent = budget.Package == EveryPackage
                // The device-wide budget is the sum of everything, including apps the policy
                // says nothing about: "two hours of screen time" means the screen, not a list.
                ? secondsToday.Values.Sum()
                : secondsToday.TryGetValue(budget.Package, out var seconds) ? seconds : 0;
            if (spent < budget.Minutes * 60) continue;
            foreach (var package in Expand([budget.Package], installed)) blocked.Add(package);
        }

        // The protected set is applied here as well as by the coordinator. Duplication on
        // purpose: a whole-device curfew expands to every installed package, and that expansion
        // is the one place a rule can reach the launcher without anybody having named it.
        return blocked.Where(p => !NeverSuspend.Contains(p)).OrderBy(p => p, StringComparer.Ordinal)
                      .ToArray();
    }

    private static IEnumerable<string> Expand(IReadOnlyList<string> packages,
        IReadOnlyList<string> installed)
    {
        foreach (var package in packages)
        {
            if (package != EveryPackage)
            {
                yield return package;
                continue;
            }
            // No inventory yet means no expansion, never "everything". Backwards, a curfew on a
            // freshly enrolled device would suspend nothing on the first pass and every app it
            // had on the second, which is the same mistake hub/policy.py refuses to make with an
            // allowlist -- and here it would fire at 22:00 with nobody watching.
            foreach (var name in installed) yield return name;
        }
    }

    /// <summary>Parse the `schedule` block of a device policy document. Never throws; a rule
    /// that will not parse is dropped rather than taking the schedule with it, because half a
    /// curfew is better than none and a malformed budget must not un-enforce a good one.</summary>
    public static DeviceSchedule Parse(JsonNode? node)
    {
        if (node is not JsonObject obj) return Empty;
        var windows = new List<TimeWindow>();
        var budgets = new List<UsageBudget>();

        if (obj["windows"] is JsonArray windowNodes)
        {
            foreach (var entry in windowNodes)
            {
                if (TimeWindow.Parse(entry) is { } window) windows.Add(window);
            }
        }
        if (obj["budgets"] is JsonArray budgetNodes)
        {
            foreach (var entry in budgetNodes)
            {
                if (UsageBudget.Parse(entry) is { } budget) budgets.Add(budget);
            }
        }
        return new DeviceSchedule(windows, budgets);
    }
}

/// <summary>
/// One curfew: on these days, between these minutes, these apps are suspended.
/// </summary>
/// <param name="Days">Indexes into a week that starts on MONDAY, matching hub/policy.py. Fixed
/// on the wire so a device in a different locale cannot read the same window differently.</param>
/// <param name="Start">Minutes past local midnight, inclusive.</param>
/// <param name="End">Minutes past local midnight, exclusive. **Less than Start means the window
/// wraps past midnight** -- 22:00 to 07:00 is the ordinary bedtime rule, and the half after
/// midnight belongs to the day AFTER the one named. Getting that wrong gives a curfew that
/// lifts at midnight, which is precisely when it is most needed and least likely to be noticed
/// by whoever set it.</param>
/// <param name="Packages">What it covers, or a single "*" for the whole device.</param>
public sealed record TimeWindow(
    IReadOnlyList<int> Days, int Start, int End, IReadOnlyList<string> Packages)
{
    private const int MinutesInDay = 24 * 60;

    /// <summary>Monday = 0, matching the wire format. DayOfWeek numbers Sunday as 0, so this is
    /// a conversion and not a cast.</summary>
    internal static int DayIndex(DayOfWeek day) => ((int)day + 6) % 7;

    public bool IsActive(DateTimeOffset localNow)
    {
        var minute = localNow.Hour * 60 + localNow.Minute;
        var today = DayIndex(localNow.DayOfWeek);

        if (End > Start)
        {
            // The simple case: entirely within one day.
            return Days.Contains(today) && minute >= Start && minute < End;
        }

        // Wrapped. Two halves, and they belong to different days: the evening half is on a day
        // the window names, and the morning half is on the day after one it names.
        if (Days.Contains(today) && minute >= Start) return true;
        var yesterday = (today + 6) % 7;
        return Days.Contains(yesterday) && minute < End;
    }

    public static TimeWindow? Parse(JsonNode? node)
    {
        if (node is not JsonObject obj) return null;
        var days = new List<int>();
        if (obj["days"] is JsonArray dayNodes)
        {
            foreach (var entry in dayNodes)
            {
                try
                {
                    var day = entry!.GetValue<int>();
                    if (day is >= 0 and < 7 && !days.Contains(day)) days.Add(day);
                }
                catch { /* one unreadable day, not a lost window */ }
            }
        }
        if (days.Count == 0) return null;

        int start, end;
        try
        {
            start = obj["start"]!.GetValue<int>();
            end = obj["end"]!.GetValue<int>();
        }
        catch { return null; }
        if (start < 0 || start >= MinutesInDay || end < 0 || end >= MinutesInDay) return null;
        // Equal bounds are ambiguous -- "no time" or "all day" -- and the hub already refuses
        // them. Dropped here too rather than guessed at.
        if (start == end) return null;

        var packages = new List<string>();
        if (obj["packages"] is JsonArray packageNodes)
        {
            foreach (var entry in packageNodes)
            {
                var name = entry?.GetValue<string>()?.Trim();
                if (!string.IsNullOrEmpty(name) && !packages.Contains(name)) packages.Add(name);
            }
        }
        return new TimeWindow(days, start, end,
            packages.Count > 0 ? packages : [DeviceSchedule.EveryPackage]);
    }
}

/// <summary>One daily allowance: this many minutes of this app, or of the device.</summary>
/// <param name="Package">A package, or "*" for total screen time.</param>
/// <param name="Minutes">Zero is meaningful and allowed -- "not at all today", which is a
/// blocklist written as a schedule.</param>
public sealed record UsageBudget(string Package, int Minutes)
{
    public static UsageBudget? Parse(JsonNode? node)
    {
        if (node is not JsonObject obj) return null;
        var package = obj["package"]?.GetValue<string>()?.Trim();
        if (string.IsNullOrEmpty(package)) package = DeviceSchedule.EveryPackage;
        try
        {
            var minutes = obj["minutes"]!.GetValue<int>();
            return minutes < 0 ? null : new UsageBudget(package, minutes);
        }
        catch { return null; }
    }
}

/// <summary>Where the agent gets the numbers a budget is checked against.
///
/// Deliberately tiny and in Core, like ILocationSource: the platform half answers "how long has
/// each app been in the foreground today, and what is today", and every decision about what
/// that means stays here.</summary>
public interface IUsageSource
{
    /// <summary>Whether usage can be read at all. False when the usage-access permission was
    /// never granted, which is a state a device sits in until somebody grants it by hand --
    /// see the Android reader. Reported to the hub so the console can say so rather than
    /// showing a budget that silently never fires.</summary>
    bool CanRead { get; }

    /// <summary>The device's own local time. Read through this rather than DateTime.Now so the
    /// evaluator is testable without pretending a workstation is in Paraguay.</summary>
    DateTimeOffset LocalNow { get; }

    /// <summary>Foreground seconds per package for the local day of <see cref="LocalNow"/>.
    /// Empty when it cannot be read; must not throw.</summary>
    IReadOnlyDictionary<string, int> SecondsToday();
}
