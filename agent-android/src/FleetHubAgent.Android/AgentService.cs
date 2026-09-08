using Android.App;
using Android.Content;
using Android.Content.PM;
using Android.OS;
using Microsoft.Extensions.Logging;
using FleetHubAgent;
using FleetHubAgent.Fleet;
using FleetHubAgent.Fleet.Executors;
using FleetHubAgent.State;
using FleetHubAgent.Telemetry;
using FleetHubAgent.Android.Platform;
using FleetHubAgent.Android.Policy;

namespace FleetHubAgent.Android;

// **Every reference to the platform namespace in this project is written global::Android.***
// and has to be. This namespace is itself called FleetHubAgent.Android, so inside it a bare
// `Android.Util.Log` resolves `Android` to THIS namespace, fails to find `.Util` in it, and
// stops there rather than falling through to the platform one. The error that produces names
// a type rather than the namespace, so it reads like a missing package.
//
// The `using Android.App;` directives above are unaffected: usings sit outside the namespace
// declaration and resolve globally. It is only bare references in code that bite.
//
// Kept rather than renamed, because the namespace matching the project name is worth more
// than the trap costs once it is written down -- but it IS a trap, so write global:: and do
// not spend twenty minutes on the error when you forget.

/// <summary>
/// The foreground service that owns the agent, and the composition root for everything under
/// it -- the counterpart of the other agents' Program.cs, which on Android has to be a Service
/// because there is no process an app can simply keep running.
///
/// **A foreground service with a visible notification is the only way to do this, not a choice
/// between options.** WorkManager's minimum interval is fifteen minutes and JobScheduler's is
/// longer under Doze; either would put a machine far outside the hub's 90-second online window,
/// so the console would show every Android device as permanently offline with occasional
/// flickers of life. The permanent notification is the price the platform charges for a process
/// that keeps running, and it is not something an app can opt out of.
///
/// **START_STICKY, and OnStartCommand is idempotent.** The system kills foreground services
/// under memory pressure and restarts them with a null Intent; the boot receiver and the
/// activity both start the service too, so it must be safe to ask for it repeatedly. Everything
/// expensive is built once in OnCreate and the loops are started at most once, guarded by
/// <see cref="_loops"/>.
/// </summary>
// All three foreground types are declared and the service picks from them at runtime -- see
// StartInForeground below. Android refuses a type the manifest does not carry, so declaring
// only the preferred one would crash the service on every device below API 34.
//
// `location` is declared but is NOT part of the steady state: the service adds it for the
// length of one locate and drops it again (BeginLocationSession). Declaring it here is what
// makes that promotion legal at all; using it permanently would put a location indicator on
// the device forever, for a capability exercised a few times a year.
//
// Two things are deliberately absent. `stopWithTask` is not set because its default is already
// false: swiping the app off the recents screen must not stop the agent, and on a device
// someone actually uses that swipe happens weekly and is not a request to stop being managed.
// The `PROPERTY_SPECIAL_USE_FGS_SUBTYPE` property is not set either -- it is a written
// justification for Play Store review, and this APK is installed by an MDM or sideloaded by the
// helpdesk, never listed. Adding one would be paperwork for a reviewer who will never see it.
[Service(
    Exported = false,
    ForegroundServiceType = ForegroundService.TypeSpecialUse
                            | ForegroundService.TypeDataSync
                            | ForegroundService.TypeLocation)]
public sealed class AgentService : Service
{
    private const int NotificationId = 1;
    private const string ChannelId = "fleethub.agent.status";

    /// <summary>How often the notification is redrawn. Deliberately far slower than the
    /// telemetry loop: the notification is a status display for someone holding the device, and
    /// rewriting it every five seconds costs a wakeup per tick to change a number nobody is
    /// watching.</summary>
    private static readonly TimeSpan NotificationRefresh = TimeSpan.FromSeconds(60);

    private ILoggerFactory? _loggerFactory;
    private ILogger<AgentService>? _log;
    private CancellationTokenSource? _cts;
    private Task? _loops;

    private AgentLoops? _agent;
    private MachineNameProvider? _names;
    private TelemetryReporter? _reporter;
    private FleetClient? _fleet;
    private ISensorSource? _sensors;

    public override IBinder? OnBind(Intent? intent) => null;

    public override void OnCreate()
    {
        base.OnCreate();

        _loggerFactory = LoggerFactory.Create(b => b.AddProvider(new LogcatLoggerProvider(LogLevel.Information)));
        _log = _loggerFactory.CreateLogger<AgentService>();
        // Published so a locate can promote this service's foreground type -- see
        // BeginLocationSession. Set before Compose so a composition failure still leaves a
        // reachable service rather than a null nobody can explain.
        _running = this;

        try
        {
            Compose();
        }
        catch (Exception e)
        {
            // A composition failure must not leave a service that is "running" and doing
            // nothing. Logged loudly and left un-started; the notification below will say the
            // agent is not running, which is the truth and is visible on the device.
            _log.LogError(e, "Could not start the agent");
        }
    }

    /// <summary>Wire the agent up. Ordering matters in two places and both are commented.
    ///
    /// Named Compose rather than Build: Android.OS.Build is in scope throughout this
    /// project, and a method of that name shadows the type for the whole class.</summary>
    private void Compose()
    {
        var store = new AndroidStateStore(this);
        var state = new AgentState(store);

        // FIRST: point the agent at its hub, before anything builds a URL. AgentConfig is
        // static and read at every call site, so a Configure that happened after FleetClient
        // was constructed would still take -- but the log line below would name the wrong hub,
        // and that line is what someone reads when a device reports nowhere.
        ManagedConfig.ApplyHubUrl(this, state);

        var identity = AndroidSystemInfo.Read(this, _loggerFactory!.CreateLogger("SystemInfo"));

        // SECOND: the machine name depends on the identity just read -- the derived default is
        // the model plus a suffix of the stable id, and getting a blank one there is what makes
        // a fleet of identical devices collapse into one machine. See MachineNaming.
        _names = new MachineNameProvider(
            state, MachineNaming.Derive(identity.Model, identity.Manufacturer, identity.SerialNumber));
        ManagedConfig.ApplyMachineName(this, _names);

        _sensors = new AndroidSensorReader(this, _loggerFactory.CreateLogger("Sensors"));
        _reporter = new TelemetryReporter(
            _loggerFactory.CreateLogger<TelemetryReporter>(), identity, _names);
        // The executors, which are the whole of what this agent can be TOLD to do today. Two
        // out of the hub's ~thirty command types, and the gap is a platform limit rather than a
        // backlog: see CommandDispatcher, which distinguishes the commands Android forbids from
        // the ones simply not written yet. The hub is told this exact set on every heartbeat
        // (AgentCapabilities), which is what stops it queueing the other twenty-eight.
        var executors = new ICommandExecutor[]
        {
            new RenameExecutor(_loggerFactory.CreateLogger<RenameExecutor>(), _names, identity),
            // Roadmap #23 phase B. Everything about what a fix MEANS is in Core, where it is
            // testable on a workstation; the two Android objects only answer "here is a
            // position, or here is why not" and "tell the person who asked".
            new LocateDeviceExecutor(
                _loggerFactory.CreateLogger<LocateDeviceExecutor>(),
                new AndroidLocationReader(this, _loggerFactory.CreateLogger("Location")),
                new LocationNotifier(this, _loggerFactory.CreateLogger("LocationNotice"))),
        };

        var dispatcher = new CommandDispatcher(
            _loggerFactory.CreateLogger<CommandDispatcher>(), executors);

        // The dispatcher is built BEFORE the client on purpose: the capability report the
        // heartbeat carries is derived from the executor set above rather than written out
        // again, so the hub cannot be told this agent runs something it has no executor for --
        // or, worse, go on refusing something it has just been given one for.
        //
        // Passed as a FUNCTION rather than a value because one of its parts is not fixed at
        // startup: `adb shell dpm set-device-owner` can grant device ownership to a process
        // that is already running, and a report captured in Compose would then claim the
        // device is unmanaged for as long as the service happened to stay up. That is the lab
        // path, and the one where a stale answer is hardest to explain.
        _fleet = new FleetClient(_loggerFactory.CreateLogger<FleetClient>(), state, _names,
            () => AgentCapabilities.For(dispatcher, ReportedFeatures()));

        // Take the device-owner powers this agent holds, if it holds any. A no-op on a device
        // that was sideloaded rather than provisioned, which is every device today -- see
        // DeviceOwner for the degradation contract the rest of the policy work is built on.
        DeviceOwner.ApplyBaseline(this, _loggerFactory.CreateLogger("DeviceOwner"));

        // The app policy this device enforces, and the report of what it managed to enforce.
        // Restored before the loops start so a process kill resumes the staleness clock where
        // it left off rather than restarting it -- see PolicyCoordinator on why that matters
        // more than resuming the enforcement itself.
        var policy = new PolicyCoordinator(
            _loggerFactory.CreateLogger<PolicyCoordinator>(), state,
            new AndroidPolicyEnforcer(this, _loggerFactory.CreateLogger("AppPolicy")));
        policy.Restore();

        // The slow local reads, on their own loop -- see AgentLoops.InventoryLoopAsync for why
        // none of this can ride the heartbeat. The coordinator is a source too: what the device
        // did with its policy is exactly the shape of every other change-only block.
        var inventory = new IInventorySource[]
        {
            new AppInventoryReader(this, _loggerFactory.CreateLogger("AppInventory")),
            policy,
        };

        _agent = new AgentLoops(
            _loggerFactory.CreateLogger<AgentLoops>(), _sensors, new AndroidUptimeSource(),
            _reporter, _fleet, dispatcher, _names,
            new AndroidEnrollmentSecretSource(this, state), inventory, policy);
    }

    /// <summary>The non-command abilities this device reports (hub/capabilities.py's FEATURE_*).
    ///
    /// `locate` is reported unconditionally, not only when the location permission happens to
    /// be granted: the feature is "this agent implements locating", and whether a fix is
    /// possible right now is the locate's own answer. Reporting it conditionally would make the
    /// console's Locate button appear and vanish as somebody toggles a system setting, which
    /// reads as a broken console rather than as a device state -- and the honest answer, "the
    /// location permission has not been granted on this device", is one the operator can only
    /// see by asking.</summary>
    private List<string> ReportedFeatures()
    {
        var features = new List<string> { AgentCapabilities.FeatureLocate };
        features.AddRange(DeviceOwner.Features(this));
        return features;
    }

    public override StartCommandResult OnStartCommand(Intent? intent, StartCommandFlags flags, int startId)
    {
        CreateNotificationChannel();

        // Before anything else that can fail. Android gives a service a few seconds to call
        // this after being started in the foreground and kills the process with a
        // ForegroundServiceDidNotStartInTimeException if it does not -- so the notification
        // goes up first and the state it describes catches up.
        StartInForeground();

        if (_agent is not null && _loops is null)
        {
            _cts = new CancellationTokenSource();
            var token = _cts.Token;
            _loops = Task.Run(() => _agent.RunAsync(token), CancellationToken.None);
            _ = Task.Run(() => NotificationLoopAsync(token), CancellationToken.None);
        }

        // Sticky: the system restarts the service after killing it for memory, which is the
        // ordinary end of an Android process rather than an exceptional one.
        return StartCommandResult.Sticky;
    }

    public override void OnDestroy()
    {
        _log?.LogInformation("Agent service stopping");
        // Cleared first: a locate racing this must find no service rather than one that is
        // half torn down and would throw from startForeground.
        if (ReferenceEquals(_running, this)) _running = null;
        try { _cts?.Cancel(); } catch { }

        // Disposed, but NOT waited on. onDestroy has a few seconds before the system kills the
        // process anyway, and blocking here to drain a held command poll would spend all of it.
        // The loops observe the token and unwind on their own.
        _sensors?.Dispose();
        _reporter?.Dispose();
        _fleet?.Dispose();
        _loggerFactory?.Dispose();

        base.OnDestroy();
    }

    // ------------------------------------------------------------------ notification

    /// <summary>Go foreground, claiming the best service type this device understands.
    ///
    /// specialUse where it exists (API 34+), dataSync from API 29, and the untyped call below
    /// that. Both types are declared in the manifest because Android refuses a type its
    /// manifest does not carry; see the comment on the class for why specialUse is preferred.
    ///
    /// **Written as one inline ladder rather than a `ForegroundType` property**, which is what
    /// it was first. The constants are themselves version-gated, so the analyzer has to be able
    /// to see the guard and the use in the same method -- behind a property it reported CA1416
    /// on every one of them, and the only ways out were a suppression or this. The Linux agent's
    /// AssemblyInfo makes the same argument from the other end: the analyzer is only worth
    /// having if its warnings are real, so the code moves rather than the warning.</summary>
    private void StartInForeground() => StartInForeground(withLocation: false);

    private void StartInForeground(bool withLocation)
    {
        var notification = BuildNotification();

        // The location type is ADDED to the base type rather than replacing it: a foreground
        // service declares everything it is doing, and this one is still reporting telemetry
        // while it takes a fix.
        if (OperatingSystem.IsAndroidVersionAtLeast(34))
            StartForeground(NotificationId, notification,
                ForegroundService.TypeSpecialUse
                | (withLocation ? ForegroundService.TypeLocation : 0));
        else if (OperatingSystem.IsAndroidVersionAtLeast(29))
            StartForeground(NotificationId, notification,
                ForegroundService.TypeDataSync
                | (withLocation ? ForegroundService.TypeLocation : 0));
        else
            StartForeground(NotificationId, notification);
    }

    // ================================================================ location promotion
    //
    // **Android 10 blocks location while an app is not visible**, unless it holds
    // ACCESS_BACKGROUND_LOCATION -- a standing grant to watch where a device goes, with a
    // prominent-disclosure obligation attached -- or it is running a foreground service started
    // with the `location` type. A managed phone is in somebody's pocket with the screen off,
    // which is precisely the blocked case, so without one of the two "find this device" answers
    // nothing at all on exactly the devices it exists for.
    //
    // This takes the second route, for the length of ONE request: the service adds the location
    // type, the fix is taken, and the type goes away again. That is a narrower thing to hold
    // than a permanent background-location grant, and it is visible -- the system shows a
    // location indicator while it is in effect. Roadmap #23 records background location as
    // deliberately still out of scope.

    /// <summary>The running service, so a locate can reach it. Set in OnCreate and cleared in
    /// OnDestroy; null when no service is up, which the caller treats as "no promotion" rather
    /// than as an error.</summary>
    private static AgentService? _running;

    /// <summary>Promote the foreground service to include the location type for the length of
    /// one request. Dispose the result to demote. Null means the promotion did not happen --
    /// the attempt goes ahead anyway, because on a device whose screen is on it works
    /// regardless, and half an answer beats none.
    ///
    /// **The caller must already have checked the runtime permission.** From Android 14,
    /// startForeground with the location type while lacking it throws SecurityException, which
    /// would take down the whole agent for the crime of being asked where it is on a device
    /// where somebody said no. See AndroidLocationReader, which checks first.</summary>
    internal static IDisposable? BeginLocationSession()
    {
        var service = _running;
        if (service is null || !OperatingSystem.IsAndroidVersionAtLeast(29)) return null;
        try
        {
            service.StartInForeground(withLocation: true);
            return new LocationSession(service);
        }
        catch (Exception e)
        {
            service._log?.LogWarning("Could not promote the service for location: {Msg}",
                e.Message);
            return null;
        }
    }

    private sealed class LocationSession(AgentService service) : IDisposable
    {
        public void Dispose()
        {
            try { service.StartInForeground(withLocation: false); }
            catch (Exception e)
            {
                // Left promoted. Not fatal and not silent: the system keeps showing a location
                // indicator, which is a support call rather than a leak, and the next service
                // start clears it.
                service._log?.LogWarning("Could not demote the service after locating: {Msg}",
                    e.Message);
            }
        }
    }

    private void CreateNotificationChannel()
    {
        // No API-26 guard: SupportedOSPlatformVersion is 26, so a notification channel always
        // exists here. A device below it could not run this app at all.
        if (GetSystemService(NotificationService) is not NotificationManager manager) return;

        // Importance Low: no sound, no heads-up banner. The notification exists because the
        // platform requires one for a service that keeps running, not because anything here
        // needs attention -- a device that pinged its holder every time it reported telemetry
        // would be uninstalled by lunchtime.
        var channel = new NotificationChannel(ChannelId, "Fleet agent", NotificationImportance.Low)
        {
            Description = "Shows that this device is being managed, and its current status.",
        };
        channel.SetShowBadge(false);
        manager.CreateNotificationChannel(channel);
    }

    private Notification BuildNotification()
    {
        // Tapping it opens the agent's own screen, which is where the hub, the machine name and
        // the enrollment state are. A notification that does nothing when tapped is the one
        // thing about this that would read as broken.
        var intent = new Intent(this, typeof(MainActivity));
        intent.SetFlags(ActivityFlags.SingleTop);
        var pending = PendingIntent.GetActivity(
            this, 0, intent, PendingIntentFlags.Immutable | PendingIntentFlags.UpdateCurrent);

        // The channel overload unconditionally, for the reason on CreateNotificationChannel:
        // this app's floor is API 26, so the deprecated channel-less builder is unreachable
        // rather than a fallback. It was here, behind a CA1422 suppression, until the floor
        // made the suppression a lie.
        return new Notification.Builder(this, ChannelId)
            .SetContentTitle("FleetHub Agent")
            .SetContentText(StatusLine())
            .SetSmallIcon(global::Android.Resource.Drawable.StatNotifySync)
            .SetContentIntent(pending)
            .SetOngoing(true)
            .SetOnlyAlertOnce(true)
            .Build();
    }

    /// <summary>What the notification says.
    ///
    /// **This is the entire on-device diagnostic surface**, so it answers the three questions
    /// someone standing next to a device actually has, in the order they matter: is it
    /// enrolled, what is it called in the console, and is it getting through. Anything longer
    /// is truncated by the system anyway.</summary>
    private string StatusLine()
    {
        if (_agent is null) return "Not running -- see logcat (tag FleetHubAgent)";

        var name = _names?.Current ?? "?";
        var enrolled = _fleet?.IsEnrolled == true;
        var buffered = _reporter?.BufferedCount ?? 0;

        // **Two independent facts, and early versions of this line reported only one.** It used
        // to answer "not enrolled" and stop, which hides whether telemetry is reaching the hub
        // -- and during a fresh install that is exactly the question being asked, because an
        // unenrolled device is the normal state for the first few minutes. Someone standing
        // over a new device could not tell "reporting fine, waiting for its secret" from
        // "cannot reach the hub at all". Found on the first real device rather than in review.
        var link = buffered > 0 ? $"offline, {buffered} buffered" : "reporting";

        return enrolled
            ? $"{name} -- {link}, managed"
            : $"{name} -- {link}, NOT enrolled (no commands)";
    }

    /// <summary>Redraw the notification periodically so its status does not go stale.
    ///
    /// Its own slow loop rather than a callback from the telemetry loop, for the same reason
    /// the agent's three loops are separate: a notification update is UI work on a system
    /// service, and a slow one must not add latency to a heartbeat.</summary>
    private async Task NotificationLoopAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try
            {
                await Task.Delay(NotificationRefresh, ct);
                if (GetSystemService(NotificationService) is NotificationManager manager)
                    manager.Notify(NotificationId, BuildNotification());
            }
            // Fully qualified: Android.OS declares an OperationCanceledException of its
            // own, so the bare name is ambiguous in any file that uses that namespace.
            catch (System.OperationCanceledException) { break; }
            catch (Exception e)
            {
                // Never fatal. A notification that stops updating is cosmetic; a loop that dies
                // taking the service with it is not.
                _log?.LogDebug("Notification refresh failed: {Msg}", e.Message);
            }
        }
    }

    // ------------------------------------------------------------------ starting

    /// <summary>Start the agent, from wherever asks -- the launcher activity, the boot
    /// receiver, or the system's own restart.
    ///
    /// StartForegroundService above API 26, which obliges the service to call startForeground
    /// within a few seconds; OnStartCommand does that first thing. Wrapped because
    /// ForegroundServiceStartNotAllowedException is thrown, not returned, when an app tries
    /// this from the background on API 31+ -- which happens if the boot broadcast is delivered
    /// late. Losing the start is recoverable (the next launch or reboot gets it); crashing the
    /// caller is not.</summary>
    public static void Start(Context context)
    {
        var intent = new Intent(context, typeof(AgentService));
        try
        {
            // Unconditionally the foreground variant: this app's floor is API 26, so
            // there is no device here that wants plain StartService.
            context.StartForegroundService(intent);
        }
        catch (Exception e)
        {
            global::Android.Util.Log.Error("FleetHubAgent", "Could not start the agent service: " + e);
        }
    }
}
