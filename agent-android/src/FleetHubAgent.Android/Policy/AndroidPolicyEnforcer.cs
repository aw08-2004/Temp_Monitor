using Android.App.Admin;
using Android.Content;
using Microsoft.Extensions.Logging;
using FleetHubAgent.Fleet;

namespace FleetHubAgent.Android.Policy;

/// <summary>
/// Suspends and un-suspends apps as the policy says -- roadmap #23 phase D.
///
/// **`setPackagesSuspended`, not `setApplicationHidden`, and the difference is the whole
/// design.** Suspension leaves the icon where it was, greyed, and the system itself shows
/// "paused by your organisation" when somebody taps it. Hiding makes an app silently vanish,
/// which reads as "uninstalled" -- and what follows is a helpdesk ticket, or somebody
/// factory-resetting a phone to get their app back. A person whose device has been restricted
/// should be able to see that it has been.
///
/// **The API returns the packages it could NOT suspend, and plumbing that back is mandatory.**
/// A policy reported as applied while three of its targets are still running is worse than no
/// policy: it is a compliance answer that is confidently wrong. Everything above this class
/// exists to carry that list to the console.
///
/// **This agent un-suspends only what it suspended.** It keeps its own record rather than
/// asking the framework which packages are suspended, because a device may have been suspended
/// by something else -- another DPC in its past, or a vendor tool -- and lifting those would be
/// this agent quietly taking ownership of decisions nobody asked it to make. What it applied,
/// it can lift; what it did not, it leaves alone.
///
/// **The never-suspend list is checked here as well as in the coordinator.** Duplication on
/// purpose: this is the last line before the framework call, and it is the one that has to hold
/// if the layers above are ever refactored into something that forgets. See NeverSuspend.
/// </summary>
public sealed class AndroidPolicyEnforcer(Context context, ILogger log) : IPolicyEnforcer
{
    /// <summary>What this agent currently has suspended. In memory only, and deliberately: on
    /// a restart it is empty, so nothing is lifted that the agent did not just apply, and the
    /// next Tick re-applies the held policy anyway. Persisting it would be persisting a claim
    /// about the framework's state that the framework is free to have changed.</summary>
    private readonly HashSet<string> _suspended = new(StringComparer.Ordinal);

    public bool CanEnforce => DeviceOwner.IsManaged(context);

    public IReadOnlyList<string> Apply(IReadOnlyList<string> packages)
    {
        var dpm = context.GetSystemService(Context.DevicePolicyService) as DevicePolicyManager;
        if (dpm is null || !CanEnforce)
        {
            // Everything asked for is reported as refused. The coordinator turns that into
            // "this device is not fully managed" rather than a list of mysterious failures.
            return packages;
        }

        var admin = DeviceOwner.Component(context);
        var wanted = packages.Where(p => !NeverSuspend.Contains(p))
                             .Distinct(StringComparer.Ordinal).ToArray();
        var failed = new List<string>();

        // Lift FIRST, then suspend. The other order leaves a window in which a package moving
        // from one policy to another is briefly suspended twice and un-suspended once, and on
        // a device that is rebooted in that window the wrong state is the one that persists.
        var release = _suspended.Except(wanted, StringComparer.Ordinal).ToArray();
        if (release.Length > 0)
        {
            foreach (var package in Suspend(dpm, admin, release, false))
            {
                // A package that cannot be UN-suspended is worth a line and nothing more: it is
                // usually one that has been uninstalled since, which is not a failure.
                log.LogInformation("Could not lift the suspension on {Package}", package);
            }
            foreach (var package in release) _suspended.Remove(package);
        }

        if (wanted.Length > 0)
        {
            var refused = Suspend(dpm, admin, wanted, true);
            failed.AddRange(refused);
            foreach (var package in wanted.Except(refused, StringComparer.Ordinal))
            {
                _suspended.Add(package);
            }
        }

        if (failed.Count > 0)
        {
            log.LogWarning("App policy: {Count} package(s) could not be suspended: {Packages}",
                failed.Count, string.Join(", ", failed));
        }
        return failed;
    }

    /// <summary>One framework call, with everything that can go wrong turned into "these were
    /// refused" rather than an exception.</summary>
    private string[] Suspend(DevicePolicyManager dpm, ComponentName admin, string[] packages,
        bool suspend)
    {
        try
        {
            // The return value is the packages it could not act on -- a package that is not
            // installed, or one the platform protects. A null return is the binding's way of
            // saying nothing was refused.
            return dpm.SetPackagesSuspended(admin, packages, suspend) ?? [];
        }
        catch (Exception e)
        {
            // The whole batch is reported as refused rather than guessed at. Reporting a
            // partial success we cannot verify is exactly the confidently-wrong compliance
            // answer this feature is built to avoid.
            log.LogWarning(e, "setPackagesSuspended({Suspend}) failed for {Count} package(s)",
                suspend, packages.Length);
            return packages;
        }
    }
}
