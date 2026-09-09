using System.Text.Json.Nodes;

namespace FleetHubAgent.Fleet;

/// <summary>
/// The app policy a device is enforcing, and everything that decides whether it still should.
///
/// The hub sends a FLAT list of packages to suspend, never a rule engine -- see hub/policy.py.
/// All the precedence, overlap and allowlist arithmetic happens there, where it can be
/// previewed before anything reaches a phone. What arrives here is the answer.
///
/// **The dead-man switch is the reason this class exists rather than a bare list.** A device
/// that stops hearing from its hub lifts every restriction on its own after
/// <see cref="MaxAge"/>. That is a deliberate asymmetry: the hub can make a device MORE
/// restricted only while it can reach it, and a phone whose hub has been decommissioned,
/// misconfigured, or put on the far side of a firewall must not be a device somebody has to
/// factory reset in order to use. The switch lives on the agent because it has to work when
/// the hub is exactly what is missing.
///
/// **`ReceivedAtUtc` is when the hub last CONFIRMED this policy, not when it was first sent.**
/// Every heartbeat that carries the same version refreshes it. Anchoring on first receipt
/// instead would expire a policy the hub has been re-affirming every ten seconds for a week,
/// which is the opposite of what the switch is for.
/// </summary>
public sealed class DevicePolicy
{
    /// <summary>Used when the hub sends a document with no age in it -- an older hub, or a
    /// malformed block. Matches hub/settings.py's policy.max_age_seconds default, so a device
    /// behaves the same whether or not it was told.</summary>
    public static readonly TimeSpan DefaultMaxAge = TimeSpan.FromDays(7);

    /// <summary>Bounds whatever the hub asked for. The FLOOR is the important one: a hub that
    /// sent zero -- through a bug, or a setting nobody sanity-checked -- would make every
    /// device lift its policy on the next tick, which is a fleet-wide un-enforcement with no
    /// operator anywhere in it.</summary>
    public static readonly TimeSpan MinMaxAge = TimeSpan.FromHours(1);
    public static readonly TimeSpan MaxMaxAge = TimeSpan.FromDays(90);

    /// <summary>The empty policy: nothing blocked. **Not the same as "no policy".** A device
    /// with no policy at all still enforces whatever it last applied; this one actively lifts
    /// it, which is what makes removing a machine from a policy able to un-block its apps.</summary>
    public static DevicePolicy Empty(DateTimeOffset now) =>
        new("", [], DeviceSchedule.Empty, DefaultMaxAge, now);

    public string Version { get; }
    public IReadOnlyList<string> Blocked { get; }

    /// <summary>Curfews and budgets (roadmap #23 phase E). Evaluated on the device against its
    /// own clock and its own usage, because the hub has neither fact promptly -- see
    /// DeviceSchedule.</summary>
    public DeviceSchedule Schedule { get; }

    public TimeSpan MaxAge { get; }
    public DateTimeOffset ReceivedAtUtc { get; private set; }

    public DevicePolicy(string version, IReadOnlyList<string> blocked, DeviceSchedule schedule,
        TimeSpan maxAge, DateTimeOffset receivedAtUtc)
    {
        Version = version ?? "";
        Blocked = blocked;
        Schedule = schedule;
        MaxAge = maxAge < MinMaxAge ? MinMaxAge : (maxAge > MaxMaxAge ? MaxMaxAge : maxAge);
        ReceivedAtUtc = receivedAtUtc;
    }

    /// <summary>The hub confirmed this same policy again. See the class summary on why this is
    /// a refresh rather than a no-op.</summary>
    public void Reaffirm(DateTimeOffset now) => ReceivedAtUtc = now;

    /// <summary>Has this policy gone stale enough to lift?</summary>
    public bool IsExpired(DateTimeOffset now) => now - ReceivedAtUtc > MaxAge;

    /// <summary>What should actually be suspended right now: the blocked list plus whatever the
    /// schedule says at this moment, minus anything protected, and nothing at all once the
    /// policy has expired.
    ///
    /// **The schedule half is re-evaluated on every call**, which is why the coordinator ticks
    /// even when nothing has arrived from the hub: a curfew starts at 22:00 with no message
    /// from anywhere, and a budget is exceeded while the device is offline.</summary>
    public IReadOnlyList<string> Effective(DateTimeOffset now, IUsageSource? usage = null,
        IReadOnlyList<string>? installed = null)
    {
        if (IsExpired(now)) return [];
        var blocked = new HashSet<string>(Blocked, StringComparer.Ordinal);
        if (usage is not null && !Schedule.IsEmpty)
        {
            foreach (var package in Schedule.Blocked(usage.LocalNow, usage.SecondsToday(),
                                                     installed ?? []))
            {
                blocked.Add(package);
            }
        }
        return blocked.Where(p => !NeverSuspend.Contains(p))
                      .OrderBy(p => p, StringComparer.Ordinal).ToArray();
    }

    /// <summary>Parse a `device_policy` block. Null when it is not one.
    ///
    /// **A malformed document is NOT read as an empty one.** Empty means "lift everything",
    /// which is a real instruction; unparseable means the hub said something this agent does
    /// not understand, and the safe response to that is to keep enforcing what is already
    /// applied until a document arrives that makes sense.</summary>
    public static DevicePolicy? Parse(JsonNode? node, string? version, DateTimeOffset now)
    {
        if (node is not JsonObject obj) return null;
        if (obj["blocked"] is not JsonArray list) return null;

        var blocked = new List<string>();
        foreach (var entry in list)
        {
            var package = entry?.GetValue<string>()?.Trim();
            if (!string.IsNullOrEmpty(package) && !blocked.Contains(package))
                blocked.Add(package);
        }

        var seconds = 0;
        if (obj["max_age_seconds"] is { } age)
        {
            try { seconds = age.GetValue<int>(); }
            catch { seconds = 0; }
        }
        var maxAge = seconds > 0 ? TimeSpan.FromSeconds(seconds) : DefaultMaxAge;
        return new DevicePolicy(version ?? "", blocked, DeviceSchedule.Parse(obj["schedule"]),
                                maxAge, now);
    }

    /// <summary>Round-trip for the state store, so a policy survives the process being killed.
    ///
    /// Persisted at all because the dead-man switch is measured from the last confirmation:
    /// an agent that forgot the policy on every restart would either re-apply from scratch
    /// (harmless) or, far worse, restart the clock and never expire on a device the hub had
    /// already stopped talking to.</summary>
    public JsonObject ToJson() => new()
    {
        ["version"] = Version,
        ["blocked"] = new JsonArray(Blocked.Select(p => (JsonNode)p!).ToArray()),
        ["schedule"] = ScheduleJson(),
        ["max_age_seconds"] = (int)MaxAge.TotalSeconds,
        ["received_at"] = ReceivedAtUtc.ToUnixTimeSeconds(),
    };

    /// <summary>The schedule, back in the shape Parse reads. Written out rather than kept as
    /// the original node because a JsonNode has a single parent, and stashing the hub's would
    /// tie this object's lifetime to the reply it came in.</summary>
    private JsonObject ScheduleJson()
    {
        var windows = new JsonArray();
        foreach (var window in Schedule.Windows)
        {
            windows.Add(new JsonObject
            {
                ["days"] = new JsonArray(window.Days.Select(d => (JsonNode)d).ToArray()),
                ["start"] = window.Start,
                ["end"] = window.End,
                ["packages"] = new JsonArray(
                    window.Packages.Select(p => (JsonNode)p!).ToArray()),
            });
        }
        var budgets = new JsonArray();
        foreach (var budget in Schedule.Budgets)
        {
            budgets.Add(new JsonObject
            {
                ["package"] = budget.Package,
                ["minutes"] = budget.Minutes,
            });
        }
        return new JsonObject { ["windows"] = windows, ["budgets"] = budgets };
    }

    public static DevicePolicy? FromJson(JsonNode? node)
    {
        if (node is not JsonObject obj) return null;
        var received = 0L;
        try { received = obj["received_at"]?.GetValue<long>() ?? 0L; } catch { received = 0L; }
        var when = received > 0
            ? DateTimeOffset.FromUnixTimeSeconds(received)
            : DateTimeOffset.UnixEpoch;
        var version = "";
        try { version = obj["version"]?.GetValue<string>() ?? ""; } catch { version = ""; }
        return Parse(obj, version, when) is { } parsed
            ? new DevicePolicy(parsed.Version, parsed.Blocked, parsed.Schedule, parsed.MaxAge,
                               when)
            : null;
    }
}

/// <summary>
/// Packages this agent will never suspend, whatever a policy says.
///
/// **This is the authoritative copy.** hub/policy.py holds one too, so the console never shows
/// an operator a policy it already knows will be partly refused -- but that copy is advice and
/// this one is enforcement. It cannot be edited from the hub, so a hub that is compromised,
/// misconfigured, or simply newer than this agent cannot brick a device. If the two disagree,
/// this one wins and the console is the thing that is wrong, which is the right way round.
///
/// Prefixes rather than exact names, because the same component ships under a different
/// package on every vendor's build: the launcher is `com.sec.android.app.launcher` on a
/// Samsung and `com.google.android.apps.nexuslauncher` on a Pixel. Suspending the launcher is
/// not a strict policy, it is a device somebody has to factory reset.
/// </summary>
public static class NeverSuspend
{
    public static readonly IReadOnlyList<string> Prefixes =
    [
        // This agent. Suspending it would end management AND remove the only channel that
        // could tell the device to stop -- the one failure with no route back but a reset.
        "net.arkeanos.fleethub",
        // The launcher, under every vendor's name for it.
        "com.android.launcher", "com.google.android.apps.nexuslauncher",
        "com.sec.android.app.launcher", "com.miui.home", "com.huawei.android.launcher",
        "com.oneplus.launcher", "com.motorola.launcher",
        // Settings, the dialer, emergency. A device that cannot be configured, cannot make a
        // call, or cannot reach emergency services is not a managed device.
        "com.android.settings", "com.android.dialer", "com.google.android.dialer",
        "com.samsung.android.dialer", "com.android.emergency", "com.android.server.telecom",
        "com.android.phone",
        // The system UI and the package installer: without these there is no status bar, no
        // notifications, and no way to install the fix.
        "com.android.systemui", "com.android.packageinstaller",
        "com.google.android.packageinstaller",
    ];

    public static bool Contains(string? package)
    {
        var name = (package ?? "").Trim().ToLowerInvariant();
        return name.Length > 0 && Prefixes.Any(name.StartsWith);
    }
}

/// <summary>Applying a policy, however the platform can.
///
/// Deliberately tiny and deliberately in Core, like ILocationSource: everything about WHAT to
/// suspend and WHETHER to is the coordinator's business, so the platform half cannot
/// accidentally decide policy.</summary>
public interface IPolicyEnforcer
{
    /// <summary>Whether this device can enforce anything at all. False on a device that is not
    /// a Device Owner, which is the ordinary state of a sideloaded build -- and the reason the
    /// coordinator reports "not fully managed" rather than a stream of failures.</summary>
    bool CanEnforce { get; }

    /// <summary>Suspend exactly <paramref name="packages"/> and un-suspend everything this
    /// agent had suspended before that is not in the list. Returns the packages it could NOT
    /// suspend, which is the whole reason this returns anything at all: a policy reported as
    /// applied while three of its targets are still running is worse than no policy.
    ///
    /// Must not throw.</summary>
    IReadOnlyList<string> Apply(IReadOnlyList<string> packages);
}
