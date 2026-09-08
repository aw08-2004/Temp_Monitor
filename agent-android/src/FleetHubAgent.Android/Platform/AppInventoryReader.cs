using System.Text.Json.Nodes;
using Android.Content;
using Android.Content.PM;
using Microsoft.Extensions.Logging;
using FleetHubAgent.Fleet;

namespace FleetHubAgent.Android.Platform;

/// <summary>
/// What is installed on this device -- roadmap #23 phase D.
///
/// **The console needs this before it can offer app policy at all.** Blocking an app means
/// naming a package, and a package name is not something an operator knows or can be expected
/// to type: `com.google.android.youtube` is guessable, `com.zhiliaoapp.musically` is TikTok.
/// So the inventory is not a nice-to-have beside the policy editor, it is what makes the policy
/// editor usable.
///
/// **Every app is reported, including system ones, and the payload says which is which.** A
/// list of only user-installed apps would be a shorter and much less useful answer: the ones a
/// helpdesk is asked to restrict most often -- the browser, the store, the camera -- ship with
/// the device. The `system` flag is what lets the console default to hiding them while an
/// operator can still find them, and it is also what keeps the never-suspend safety list
/// meaningful.
///
/// **`suspended` and `enabled` are read back from the framework, not assumed from policy.**
/// That is the entire point of reporting them: `setPackagesSuspended` returns the packages it
/// could NOT suspend, and a policy that silently failed on three of them while reporting
/// success is worse than no policy. The console compares what it asked for against what the
/// device says is true.
///
/// **`QUERY_ALL_PACKAGES` is not requested, and is not needed.** Android 11 hid the full
/// package list from ordinary apps behind that permission, which is a Play policy problem and
/// a broad one. A device owner is exempt: `getInstalledPackages` returns everything for a DPC
/// without it. So on an unmanaged device this reader sees only its own package and whatever
/// the platform chooses to reveal -- which is honest, and reads in the console as a device that
/// is not fully managed rather than as a device with no apps.
/// </summary>
public sealed class AppInventoryReader(Context context, ILogger log) : IInventorySource
{
    /// <summary>Matches the heartbeat key hub/fleet_web.py ingests. A mismatch here is a block
    /// the hub accepts, stores nowhere, and never mentions.</summary>
    public string Key => "apps";

    /// <summary>Fifteen minutes. Installing or removing an app is a human-paced event and
    /// nobody is watching this list refresh; enumerating every package is a walk over the whole
    /// device, which is why it is on this loop and not the heartbeat. Applying a policy calls
    /// Invalidate rather than waiting this out.</summary>
    public TimeSpan RefreshInterval => TimeSpan.FromMinutes(15);

    /// <summary>Bounds a device with an implausible number of packages. A phone has 150-400;
    /// this is a ceiling on a pathological case, not a limit anybody will meet.</summary>
    private const int MaxApps = 1000;

    public JsonObject? Read()
    {
        var manager = context.PackageManager;
        if (manager is null)
        {
            log.LogWarning("No package manager; cannot read the app inventory");
            return null;
        }

        List<PackageInfo> packages;
        try
        {
            packages = Installed(manager);
        }
        catch (Exception e)
        {
            // Null, NOT an empty list. "I could not look" and "I looked and there is nothing"
            // are different reports, and the hub would believe the second -- wiping a good
            // inventory on one transient failure. See IInventorySource.
            log.LogWarning(e, "Could not enumerate installed packages");
            return null;
        }

        var apps = new JsonArray();
        foreach (var package in packages.Take(MaxApps))
        {
            var app = Describe(manager, package);
            if (app is not null) apps.Add(app);
        }

        log.LogInformation("App inventory: {Count} package(s)", apps.Count);
        // An OBJECT wrapping the array, always. The hub tests this key with `is not None`, and
        // an object stays truthy when the list inside it is empty where a bare `[]` would be
        // dropped by any truthiness check on the way -- which would make "this device now has
        // no apps" the one report that could never arrive. PatchInventoryReporter's lesson.
        return new JsonObject { ["apps"] = apps };
    }

    private static List<PackageInfo> Installed(PackageManager manager)
    {
        // The flags overload is deprecated from API 33 in favour of a PackageInfoFlags object,
        // and the replacement does not exist below it. Both branches ask for exactly the same
        // thing; the split is a binding detail, not a behaviour difference.
        if (OperatingSystem.IsAndroidVersionAtLeast(33))
        {
            // 0L, explicitly: Of() is overloaded on both `long` and the PackageInfoFlagsLong
            // enum, and a bare 0 is ambiguous between them.
            return manager.GetInstalledPackages(
                PackageManager.PackageInfoFlags.Of(0L))?.ToList() ?? [];
        }
#pragma warning disable CA1422 // the pre-33 overload, reached only below 33
        return manager.GetInstalledPackages(PackageInfoFlags.MatchAll)?.ToList() ?? [];
#pragma warning restore CA1422
    }

    private JsonObject? Describe(PackageManager manager, PackageInfo package)
    {
        var name = package.PackageName;
        if (string.IsNullOrEmpty(name)) return null;

        var info = package.ApplicationInfo;
        // The launcher label, which is what a person recognises. Falling back to the package
        // name rather than to an empty string: a row with no label at all is one an operator
        // cannot act on, and "com.zhiliaoapp.musically" beats a blank.
        var label = name;
        try
        {
            if (info is not null) label = manager.GetApplicationLabel(info) ?? name;
        }
        catch (Exception)
        {
            // A package whose resources cannot be loaded -- mid-uninstall, or on a profile this
            // process cannot read. Not worth a log line per app; the package name still goes.
        }

        var flags = info?.Flags ?? 0;
        return new JsonObject
        {
            ["package"] = name,
            ["label"] = label,
            // UpdatedSystemApp as well as SystemApp: an updated Chrome or WebView is still a
            // system app for every purpose that matters here, and reporting it as user-installed
            // would put it in the list an operator is invited to block freely.
            ["system"] = flags.HasFlag(ApplicationInfoFlags.System)
                         || flags.HasFlag(ApplicationInfoFlags.UpdatedSystemApp),
            // Read back from the framework, never assumed from the policy that asked for it --
            // see the class summary. This is what makes a partially-applied policy visible.
            ["enabled"] = info?.Enabled ?? true,
            ["suspended"] = IsSuspended(info),
            ["version"] = package.VersionName ?? "",
        };
    }

    private static bool IsSuspended(ApplicationInfo? info)
    {
        // FLAG_SUSPENDED arrived with the suspension API itself (API 24), below this app's own
        // floor of 26 -- so no version guard is needed and none is written. The property is
        // read through Flags rather than a dedicated binding because the binding does not
        // expose one.
        const ApplicationInfoFlags Suspended = (ApplicationInfoFlags)0x40000000;
        return info is not null && info.Flags.HasFlag(Suspended);
    }
}
