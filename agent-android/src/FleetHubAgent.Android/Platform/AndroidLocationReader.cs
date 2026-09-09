using Android.Content;
using Android.Content.PM;
using Android.Locations;
using Android.OS;
using Microsoft.Extensions.Logging;
using FleetHubAgent.Fleet.Executors;

namespace FleetHubAgent.Android.Platform;

/// <summary>
/// One position from the framework's own <c>LocationManager</c> -- roadmap #23 phase B.
///
/// **Framework LocationManager, not Google Play Services.** `FusedLocationProviderClient` is
/// technically better: it blends sensors, it is faster indoors, and it is what every tutorial
/// uses. It would also be the first Google dependency in a repo that vendors nothing from
/// Google, it drags in a large transitive binding graph, and it makes the agent require that
/// Play Services is present and current -- which fails on de-Googled builds and on exactly the
/// cheap tablets a fleet buys by the dozen. Slower and worse indoors is an acceptable trade for
/// a human-paced "find this device" with a 45-second budget; a device the agent cannot run on
/// at all is not.
///
/// **Both providers are asked at once, and the best answer within the budget wins.** Taking the
/// first fix would always take the network one: it arrives in about a second with a radius of
/// several hundred metres, while GPS takes tens of seconds and lands inside ten. For "find this
/// device" a kilometre-wide circle is often not an answer, so this waits out the budget for
/// something better -- unless a fix arrives that is already good enough to stop for.
///
/// **A last known position is returned rather than nothing, and flagged `stale`.** A phone in a
/// drawer with no sky and no network still has a position from this morning, and for the case
/// this feature exists for that is enormously useful. It is only useful if it is LABELLED --
/// see LocateDeviceExecutor.Describe, where `stale` is called the most important field in the
/// payload.
///
/// **Background access comes from the foreground service, not from a background permission.**
/// Android 10+ blocks location while an app is not visible unless it holds
/// `ACCESS_BACKGROUND_LOCATION` -- which carries a prominent-disclosure obligation and is a
/// standing grant -- OR it is running a foreground service started with the `location` type.
/// This takes the second route: the service is promoted to include that type for the length of
/// one locate and demoted immediately after, so the device can be found with its screen off
/// without the agent holding a permanent right to watch where it goes. Roadmap #23 records
/// background location as deliberately still out of scope.
/// </summary>
public sealed class AndroidLocationReader(Context context, ILogger log) : ILocationSource
{
    /// <summary>Stop early for a fix at least this good. Ten metres is a GPS fix with a clear
    /// view; there is nothing to gain by holding a wake lock for another thirty seconds to
    /// improve on it, and the device is in somebody's pocket.</summary>
    private const float GoodEnoughMetres = 25f;

    /// <summary>Below this, an "accuracy" is not a measurement. Some providers report 0 for
    /// "unknown", which would otherwise read as a perfect fix and win every comparison.</summary>
    private const float ImplausibleAccuracyMetres = 1f;

    private string? _reason;

    public string? UnavailableReason => _reason;

    public async Task<LocationFix?> GetCurrentAsync(TimeSpan budget, CancellationToken ct)
    {
        _reason = null;

        if (context.GetSystemService(Context.LocationService) is not LocationManager manager)
        {
            _reason = "this device has no location service";
            return null;
        }

        // Checked BEFORE the service is promoted, and that order is load-bearing: from
        // Android 14, calling startForeground with the location type while lacking the runtime
        // permission throws SecurityException -- which would take down the whole agent for the
        // crime of being asked where it is on a device where somebody said no.
        if (!HasLocationPermission())
        {
            _reason = "the location permission has not been granted on this device";
            return null;
        }

        var providers = EnabledProviders(manager);
        if (providers.Count == 0)
        {
            // Location switched off entirely. The last known position may still be there and
            // is worth far more than nothing -- but it is old, and says so.
            var remembered = LastKnown(manager);
            if (remembered is not null) return remembered;
            _reason = "location is switched off on this device";
            return null;
        }

        // Promoted for the length of this one request. Null when the promotion failed or the
        // platform does not need it; either way the attempt goes ahead, because on a device
        // whose screen is on it will work regardless and half an answer beats none.
        using var session = AgentService.BeginLocationSession();

        var best = await ListenAsync(manager, providers, budget, ct);
        if (best is not null) return best;

        var fallback = LastKnown(manager);
        if (fallback is not null) return fallback;
        _reason = $"no position within {(int)budget.TotalSeconds} seconds, and the device has " +
                  $"no remembered position";
        return null;
    }

    private bool HasLocationPermission()
        => context.CheckSelfPermission(global::Android.Manifest.Permission.AccessFineLocation)
               == Permission.Granted
           || context.CheckSelfPermission(global::Android.Manifest.Permission.AccessCoarseLocation)
               == Permission.Granted;

    private static List<string> EnabledProviders(LocationManager manager)
    {
        // GPS first so a caller reading the list sees the preference, though both are listened
        // to simultaneously. The passive provider is deliberately absent: it only reports what
        // some OTHER app happened to request, so on a device where nothing else asks for
        // location it never fires, and it would look like a working provider that is silent.
        var wanted = new[] { LocationManager.GpsProvider, LocationManager.NetworkProvider };
        var enabled = new List<string>();
        foreach (var provider in wanted)
        {
            try
            {
                if (manager.IsProviderEnabled(provider)) enabled.Add(provider);
            }
            catch (Exception)
            {
                // A device without that provider at all. Not an error; it is simply not there.
            }
        }
        return enabled;
    }

    /// <summary>Listen on every enabled provider until the budget runs out or a fix arrives
    /// that is good enough to stop for. Returns the most accurate fix seen, or null.</summary>
    private async Task<LocationFix?> ListenAsync(
        LocationManager manager, List<string> providers, TimeSpan budget, CancellationToken ct)
    {
        var completion = new TaskCompletionSource<bool>(
            TaskCreationOptions.RunContinuationsAsynchronously);
        var listener = new FixListener(location =>
        {
            // Signalled rather than returned: several providers are running and the first
            // GOOD one ends the wait, while a poor one is kept and the wait continues.
            if (IsGoodEnough(location)) completion.TrySetResult(true);
        });

        var registered = new List<string>();
        foreach (var provider in providers)
        {
            try
            {
                // minTime 0 / minDistance 0: this is a one-shot request bounded by the budget
                // below, not a subscription, so throttling it would only delay the answer.
                // An explicit Looper because this runs on a pool thread and the two-argument
                // overload would try to use the calling thread's, which has none.
                manager.RequestLocationUpdates(provider, 0L, 0f, listener, Looper.MainLooper!);
                registered.Add(provider);
            }
            catch (Exception e)
            {
                log.LogDebug("Location provider {Provider} refused updates: {Msg}",
                    provider, e.Message);
            }
        }
        if (registered.Count == 0)
        {
            _reason = "no location provider on this device would answer";
            return null;
        }

        try
        {
            using var deadline = CancellationTokenSource.CreateLinkedTokenSource(ct);
            deadline.CancelAfter(budget);
            // Waits out the whole budget unless a good-enough fix ends it early. The
            // cancellation is the ORDINARY exit, not an error: most locates end here with
            // whatever the listener collected along the way.
            await completion.Task.WaitAsync(deadline.Token);
        }
        catch (Exception) when (!ct.IsCancellationRequested)
        {
            // Budget expired. Whatever the listener saw is still the answer.
        }
        finally
        {
            try { manager.RemoveUpdates(listener); }
            catch (Exception e)
            {
                // Leaking a listener would leave the GPS running on a device in somebody's
                // pocket, which is a battery complaint rather than a bug report -- worth a
                // line even though nothing can be done about it here.
                log.LogWarning("Could not stop location updates: {Msg}", e.Message);
            }
        }

        var location = listener.Best;
        return location is null ? null : Convert(location, stale: false);
    }

    private static bool IsGoodEnough(Location location)
        => location.HasAccuracy && location.Accuracy >= ImplausibleAccuracyMetres
                                && location.Accuracy <= GoodEnoughMetres;

    private LocationFix? LastKnown(LocationManager manager)
    {
        Location? best = null;
        foreach (var provider in new[] { LocationManager.GpsProvider,
                                         LocationManager.NetworkProvider })
        {
            try
            {
                var candidate = manager.GetLastKnownLocation(provider);
                // NEWEST wins here, not most accurate -- the opposite of the live comparison
                // above, and deliberately. A precise fix from yesterday is worse than a vague
                // one from ten minutes ago when somebody is trying to find a device now.
                if (candidate is not null && (best is null || candidate.Time > best.Time))
                    best = candidate;
            }
            catch (Exception e)
            {
                log.LogDebug("No last known location from {Provider}: {Msg}",
                    provider, e.Message);
            }
        }
        return best is null ? null : Convert(best, stale: true);
    }

    private static LocationFix Convert(Location location, bool stale) => new(
        Latitude: location.Latitude,
        Longitude: location.Longitude,
        // Null rather than 0 when the platform did not say, and null for an implausible
        // reading: the console draws a confidence circle from this number, and a zero-radius
        // circle claims a precision no consumer GPS has.
        AccuracyMetres: location.HasAccuracy && location.Accuracy >= ImplausibleAccuracyMetres
            ? location.Accuracy
            : null,
        Provider: location.Provider ?? "unknown",
        // Location.Time is milliseconds since the epoch, in WALL CLOCK time -- so it is what
        // the device's clock said, which is the same clock the hub compares against. Divided
        // rather than recomputed from `now`, because for a stale fix the difference between
        // when it was taken and when it was reported is the entire point.
        FixedAtUnix: location.Time / 1000,
        Stale: stale);

    /// <summary>Collects fixes from every provider and keeps the most accurate one.
    ///
    /// A private listener class rather than a lambda because RemoveUpdates must be handed the
    /// SAME instance that was registered, and several providers share one here on purpose --
    /// registering one listener per provider would make the comparison below somebody's job to
    /// reassemble.</summary>
    private sealed class FixListener(Action<Location> onFix) : Java.Lang.Object, ILocationListener
    {
        private readonly object _gate = new();
        public Location? Best { get; private set; }

        public void OnLocationChanged(Location location)
        {
            lock (_gate)
            {
                if (Best is null || Better(location, Best)) Best = location;
            }
            onFix(location);
        }

        // A fix WITHOUT a stated accuracy never displaces one that has it: "we do not know how
        // wrong this is" cannot be an improvement on a number.
        private static bool Better(Location candidate, Location incumbent)
            => candidate.HasAccuracy
               && (!incumbent.HasAccuracy || candidate.Accuracy < incumbent.Accuracy);

        // The three the interface requires and this reader has no use for. A provider going
        // away mid-request is not worth reacting to: the budget below is the timeout either
        // way, and the last known position is the fallback either way. OnStatusChanged has been
        // deprecated since API 29 and is never called on modern devices at all, but the binding
        // still requires it.
        public void OnProviderDisabled(string provider) { }
        public void OnProviderEnabled(string provider) { }
        public void OnStatusChanged(string? provider, Availability status, Bundle? extras) { }
    }
}
