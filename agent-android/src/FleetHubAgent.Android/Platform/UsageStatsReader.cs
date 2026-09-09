using System.Text.Json.Nodes;
using Android.App;
using Android.App.Usage;
using Android.Content;
using Android.Content.PM;
using Microsoft.Extensions.Logging;
using FleetHubAgent.Fleet;

namespace FleetHubAgent.Android.Platform;

/// <summary>
/// How long each app has been in the foreground -- roadmap #23 phase E.
///
/// **Two jobs on purpose, because they are the same read.** As an
/// <see cref="IUsageSource"/> it answers the question a budget asks every minute, on the
/// device, with no hub involved. As an <see cref="IInventorySource"/> it ships the same numbers
/// to the console so somebody can see where a day went. Splitting them would mean querying the
/// platform twice for one answer, and worse, would let the number a budget enforced differ from
/// the number the console showed.
///
/// **`PACKAGE_USAGE_STATS` is an appop, not a runtime permission, and a Device Owner cannot
/// grant it silently.** `setPermissionGrantState` covers dangerous runtime permissions; usage
/// access is one of the special-access screens, reached only by a person walking into
/// Settings. Every version of this that assumed otherwise would have produced a fleet of
/// devices reporting nothing and enforcing every budget as "zero minutes used" -- which is the
/// quiet failure, because a budget that never fires looks exactly like a budget nobody
/// exceeded. So the state is DETECTED (<see cref="CanRead"/>), reported to the hub as a
/// capability, and surfaced on the device's own setup screen with a button that opens the
/// right settings page.
///
/// **A day is the DEVICE's local day.** The same reasoning as DeviceSchedule: a budget is
/// somebody's day, not the hub's, and a device that travels must not get two Mondays or none.
/// The day key is `YYYY-MM-DD` in local time, which is what hub/usage.py stores and what its
/// retention prune compares.
///
/// **Foreground time is summed from events, not from `queryUsageStats`.** The daily buckets
/// that API returns are maintained lazily and can span more than the interval asked for, so
/// "today" from it is somewhere between accurate and a day and a half. Resume/pause events are
/// exact, and exactness is what a budget needs at the moment it decides to suspend something
/// somebody is using.
/// </summary>
public sealed class UsageStatsReader(Context context, ILogger log) : IUsageSource, IInventorySource
{
    /// <summary>Matches the heartbeat key hub/fleet_web.py ingests.</summary>
    public string Key => "usage";

    /// <summary>Half an hour. The console's usage view is a "where did the day go" read, not a
    /// live one, and the numbers only grow -- so a late report loses nothing. Enforcement does
    /// not wait for this: budgets are checked from <see cref="SecondsToday"/> on the policy
    /// tick, once a minute.</summary>
    public TimeSpan RefreshInterval => TimeSpan.FromMinutes(30);

    /// <summary>How much history each report carries. Backfill exists for the device that
    /// spent a week in a drawer: the hub merges by day, so an old day arriving late fills a
    /// gap rather than replacing anything. Seven because the platform's own event buffer is
    /// trimmed at about that, so asking for more would send empty days that overwrite
    /// nothing and cost a row each.</summary>
    private const int DaysReported = 7;

    /// <summary>The appop's string name. Written out rather than taken from
    /// AppOpsManager.OpstrGetUsageStats because that binding is API 29+ and this app runs from
    /// 26; the string itself is stable platform API and has never changed.</summary>
    private const string UsageStatsOp = "android:get_usage_stats";

    /// <summary>Long enough that the once-a-minute policy tick costs one platform query per
    /// tick rather than several, short enough that a budget bites within a tick of being
    /// spent. Not a correctness knob: a stale read here only ever delays enforcement by less
    /// than the tick that would have applied it.</summary>
    private static readonly TimeSpan CacheFor = TimeSpan.FromSeconds(45);

    private readonly object _gate = new();
    /// <summary>Local day, then package, then seconds. **A high-water mark, never a plain
    /// assignment.** The platform trims its event buffer while a day is still running, so a
    /// later query for the same day can return a SMALLER number than an earlier one -- and a
    /// budget that reads a smaller number un-suspends an app somebody has already spent their
    /// day in. Rebuilt from the platform after a process restart, which is the one gap: the
    /// events for the current day survive a restart, so the mark is re-derived rather than
    /// persisted.</summary>
    private readonly Dictionary<string, Dictionary<string, int>> _totals = new(StringComparer.Ordinal);
    private DateTimeOffset _readAt = DateTimeOffset.MinValue;

    /// <summary>The device's own local time. Everything about a day and a curfew hangs off
    /// this, and the Device Owner enforces automatic time so it is not the person's to move
    /// (see DeviceOwner.ApplyBaseline).</summary>
    public DateTimeOffset LocalNow => DateTimeOffset.Now;

    /// <summary>Whether usage access has been granted on this device.
    ///
    /// Evaluated live rather than cached at startup, because it is granted by a person in
    /// Settings while this process is running -- caching it would leave the agent claiming it
    /// cannot read usage for as long as the service happened to stay up, which on a managed
    /// device is until the next reboot.
    /// </summary>
    public bool CanRead => IsGranted(context);

    /// <summary>The same question without a reader, for the setup screen -- which asks it to
    /// tell a technician holding the device whether the switch they just flipped took.</summary>
    public static bool IsGranted(Context? context)
    {
        try
        {
            var ops = context?.GetSystemService(Context.AppOpsService) as AppOpsManager;
            var package = context?.PackageName;
            if (ops is null || context is null || string.IsNullOrEmpty(package)) return false;
            var uid = global::Android.OS.Process.MyUid();

            // checkOpNoThrow was renamed unsafeCheckOpNoThrow in API 29 with identical
            // behaviour, and API 36 deprecated that in turn. Both spellings are suppressed
            // rather than chased: there is no form of this question that is current across
            // 26 to 36, the two that exist behave identically, and the alternative -- noting
            // the op instead of checking it -- writes an access record for a check that is
            // asking whether an access is possible at all.
#pragma warning disable CA1422
            var mode = OperatingSystem.IsAndroidVersionAtLeast(29)
                ? ops.UnsafeCheckOpNoThrow(UsageStatsOp, uid, package)
                : ops.CheckOpNoThrow(UsageStatsOp, uid, package);
#pragma warning restore CA1422

            // MODE_DEFAULT means "nobody has decided", and the platform then falls back to the
            // manifest permission -- which is why the permission is declared even though it can
            // never be granted at install time.
            if (mode == AppOpsManagerMode.Default)
            {
                return context.CheckSelfPermission(
                    global::Android.Manifest.Permission.PackageUsageStats) == Permission.Granted;
            }
            return mode == AppOpsManagerMode.Allowed;
        }
        catch (Exception)
        {
            // A build with no app-ops service, which is not a device this feature works on.
            // False is both true and the safe direction: nothing is queried and the hub is told
            // so.
            return false;
        }
    }

    /// <summary>Foreground seconds per package for the device's local day. Empty when usage
    /// cannot be read, which is a real state and not an error -- see the class summary.</summary>
    public IReadOnlyDictionary<string, int> SecondsToday()
    {
        var now = LocalNow;
        Refresh(now);
        lock (_gate)
        {
            return _totals.TryGetValue(DayKey(now), out var today)
                ? new Dictionary<string, int>(today, StringComparer.Ordinal)
                : new Dictionary<string, int>(StringComparer.Ordinal);
        }
    }

    /// <summary>The `usage` block, in the shape hub/usage.py's record_usage reads.
    ///
    /// Null when usage cannot be read, never an empty ledger. "I was not allowed to look" and
    /// "this device was not used today" are different reports and the second is the one a
    /// console would believe.</summary>
    public JsonObject? Read()
    {
        if (!CanRead)
        {
            log.LogInformation("Usage access has not been granted; reporting no usage");
            return null;
        }

        var now = LocalNow;
        Refresh(now, force: true);

        var days = new JsonObject();
        lock (_gate)
        {
            foreach (var (day, packages) in _totals.OrderBy(p => p.Key, StringComparer.Ordinal))
            {
                var totals = new JsonObject();
                foreach (var (package, seconds) in packages.OrderBy(p => p.Key, StringComparer.Ordinal))
                {
                    if (seconds > 0) totals[package] = seconds;
                }
                if (totals.Count > 0) days[day] = totals;
            }
        }

        log.LogInformation("Usage: {Days} day(s) reported", days.Count);
        // An OBJECT around the days, for the same reason AppInventoryReader wraps its array:
        // the hub tests this block with a truthiness check, and a device whose ledger is empty
        // must still be able to say so.
        return new JsonObject { ["days"] = days };
    }

    // ---------------------------------------------------------------- reading the platform

    private void Refresh(DateTimeOffset now, bool force = false)
    {
        lock (_gate)
        {
            if (!force && now - _readAt < CacheFor) return;
            _readAt = now;
        }

        var manager = context.GetSystemService(Context.UsageStatsService) as UsageStatsManager;
        if (manager is null)
        {
            log.LogWarning("No usage stats service on this device");
            return;
        }

        // The whole window in one query, then bucketed by the local day each session STARTED
        // in. Attributing by start rather than splitting at midnight keeps a session that runs
        // past midnight in one day: a budget is about an evening, and a phone put down at
        // 00:20 has not been handed a fresh allowance twenty minutes earlier than everyone
        // else's.
        var from = StartOfDay(now.AddDays(-(DaysReported - 1)));
        var collected = new Dictionary<string, Dictionary<string, int>>(StringComparer.Ordinal);
        try
        {
            Collect(manager, from, now, collected);
        }
        catch (Exception e)
        {
            // Never fatal, and never clears what is already held: the previous totals are the
            // better answer to a failed query, and they are what a budget is enforcing.
            log.LogWarning(e, "Could not read usage events");
            return;
        }

        lock (_gate)
        {
            foreach (var (day, packages) in collected)
            {
                if (!_totals.TryGetValue(day, out var held))
                {
                    held = new Dictionary<string, int>(StringComparer.Ordinal);
                    _totals[day] = held;
                }
                foreach (var (package, seconds) in packages)
                {
                    // The high-water mark. See _totals on why this is a max and not a set.
                    if (!held.TryGetValue(package, out var previous) || seconds > previous)
                        held[package] = seconds;
                }
            }

            // Drop days that have fallen out of the reported window, so a device left running
            // for a year does not accumulate a year of dictionaries.
            var oldest = DayKey(now.AddDays(-(DaysReported - 1)));
            foreach (var day in _totals.Keys.Where(
                         d => string.CompareOrdinal(d, oldest) < 0).ToList())
            {
                _totals.Remove(day);
            }
        }
    }

    private void Collect(UsageStatsManager manager, DateTimeOffset from, DateTimeOffset now,
        Dictionary<string, Dictionary<string, int>> into)
    {
        var events = manager.QueryEvents(from.ToUnixTimeMilliseconds(), now.ToUnixTimeMilliseconds());
        if (events is null) return;

        // Package to the millisecond it came to the foreground and the day that moment fell in.
        var open = new Dictionary<string, (long At, string Day)>(StringComparer.Ordinal);
        var entry = new UsageEvents.Event();

        while (events.HasNextEvent)
        {
            if (!events.GetNextEvent(entry)) break;
            var package = entry.PackageName;
            if (string.IsNullOrEmpty(package)) continue;

            // The event-type NUMBERS rather than the binding's names. ACTIVITY_RESUMED (1) and
            // ACTIVITY_PAUSED (2) are API 29 renames of MOVE_TO_FOREGROUND and
            // MOVE_TO_BACKGROUND, which carry the same values and are deprecated -- so every
            // named form is either too new for this app's floor of 26 or raises an obsolete
            // warning on every device above it. The values have never moved.
            var type = (int)entry.EventType;
            if (type == 1)
            {
                open[package] = (entry.TimeStamp, DayKey(Local(entry.TimeStamp)));
            }
            else if (type == 2 && open.Remove(package, out var started))
            {
                Add(into, started.Day, package, entry.TimeStamp - started.At);
            }
        }

        // Whatever is still in the foreground when the query ends. Without this, the app
        // somebody is using right now contributes nothing until they put it down -- which is
        // exactly the app a budget is about to run out on.
        var end = now.ToUnixTimeMilliseconds();
        foreach (var (package, started) in open)
        {
            Add(into, started.Day, package, end - started.At);
        }
    }

    private static void Add(Dictionary<string, Dictionary<string, int>> into, string day,
        string package, long milliseconds)
    {
        if (milliseconds <= 0) return;
        if (!into.TryGetValue(day, out var packages))
        {
            packages = new Dictionary<string, int>(StringComparer.Ordinal);
            into[day] = packages;
        }
        var seconds = (int)Math.Min(milliseconds / 1000, int.MaxValue);
        packages[package] = packages.TryGetValue(package, out var held) ? held + seconds : seconds;
    }

    private static DateTimeOffset Local(long unixMilliseconds)
        => DateTimeOffset.FromUnixTimeMilliseconds(unixMilliseconds).ToLocalTime();

    private static DateTimeOffset StartOfDay(DateTimeOffset local)
        => new(local.Year, local.Month, local.Day, 0, 0, 0, local.Offset);

    /// <summary>`YYYY-MM-DD` local, which is the key hub/usage.py stores and prunes on.
    /// Invariant culture explicitly: a device set to a non-Gregorian calendar would otherwise
    /// produce a day key the hub drops as unparseable, silently, forever.</summary>
    private static string DayKey(DateTimeOffset local)
        => local.ToString("yyyy-MM-dd", System.Globalization.CultureInfo.InvariantCulture);
}
