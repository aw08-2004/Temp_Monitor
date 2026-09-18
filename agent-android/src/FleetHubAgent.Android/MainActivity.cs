using Android.App;
using Android.Content;
using Android.Content.PM;
using Android.OS;
using Android.Text;
using Android.Views;
using Android.Widget;
using FleetHubAgent;
using FleetHubAgent.State;
using FleetHubAgent.Android.Platform;
using FleetHubAgent.Android.Policy;
using Microsoft.Extensions.Logging;

namespace FleetHubAgent.Android;

/// <summary>
/// The device's only screen: what this device is called in the console, which hub it reports
/// to, and whether it is enrolled -- plus the two fields someone has to fill in when there is
/// no MDM to fill them in for them.
///
/// **This is a setup and diagnostics screen, not an app.** Nobody uses it after the first
/// minute; its job is to make the two states that look identical from outside
/// distinguishable -- "reporting and taking commands" versus "reporting, and silently ignoring
/// every command because it never enrolled". That second state is the one the Linux agent's
/// installer prints a warning about, and on a phone there is no installer output to print it
/// in, so it is on screen instead.
///
/// **The UI is built in code rather than inflated from a layout.** Six widgets, no styling, no
/// configuration variants, and nothing here is ever redesigned -- an XML layout would add a
/// resource file and a set of generated ids to keep in step for no benefit anyone would ever
/// see. That reasoning does not generalise to a real app screen, and this file is the only
/// place in this project it applies.
/// </summary>
[Activity(
    Label = "FleetHub Agent",
    MainLauncher = true,
    Exported = true,
    LaunchMode = LaunchMode.SingleTask)]
public sealed class MainActivity : Activity
{
    private const int RequestPostNotifications = 1001;
    private const int RequestLocation = 1002;

    private AgentState _state = null!;
    private TextView _status = null!;
    private EditText _hubField = null!;
    private EditText _secretField = null!;

    protected override void OnCreate(Bundle? savedInstanceState)
    {
        base.OnCreate(savedInstanceState);

        _state = new AgentState(new AndroidStateStore(this));
        ManagedConfig.ApplyHubUrl(this, _state);

        SetContentView(BuildLayout());

        // Asked here rather than from the service, because a runtime permission can only be
        // requested from an activity. On Android 13+ the foreground notification does not
        // appear without it -- the service still RUNS, so this is not fatal, but the only
        // status display the device has would be invisible.
        RequestNotificationPermissionIfNeeded();

        // Starting on every launch, not only the first: this is also the recovery path when an
        // OEM battery manager has killed the service, and tapping the icon is what someone
        // will try first.
        AgentService.Start(this);
    }

    protected override void OnResume()
    {
        base.OnResume();
        RefreshStatus();
    }

    // ------------------------------------------------------------------ layout

    private View BuildLayout()
    {
        var root = new LinearLayout(this) { Orientation = Orientation.Vertical };
        root.SetPadding(48, 48, 48, 48);

        _status = new TextView(this);
        _status.SetPadding(0, 0, 0, 48);
        root.AddView(_status);

        root.AddView(Label("Hub URL"));
        _hubField = new EditText(this)
        {
            Hint = AgentConfig.DefaultHubBase,
            InputType = InputTypes.TextVariationUri,
            Text = _state.LoadHubBaseOverride() ?? "",
        };
        root.AddView(_hubField);

        root.AddView(Label("Enrollment secret"));
        _secretField = new EditText(this)
        {
            // The hub's AGENT_ENROLLMENT_SECRET, one shared value for the whole fleet. Masked
            // because it is a fleet credential being typed on a device somebody else may be
            // looking at, and never re-displayed once stored -- the field below shows whether
            // one is held, not what it is.
            Hint = "Shared AGENT_ENROLLMENT_SECRET",
            InputType = InputTypes.TextVariationPassword | InputTypes.ClassText,
        };
        root.AddView(_secretField);

        var save = new Button(this) { Text = "Save and enroll" };
        save.Click += (_, _) => Save();
        root.AddView(save);

        var battery = new Button(this) { Text = "Exclude from battery optimisation" };
        battery.Click += (_, _) => OpenBatterySettings();
        root.AddView(battery);

        var usage = new Button(this) { Text = "Grant usage access" };
        usage.Click += (_, _) => OpenUsageAccessSettings();
        root.AddView(usage);

        var location = new Button(this) { Text = "Grant location" };
        location.Click += (_, _) => GrantLocation();
        root.AddView(location);

        var hint = new TextView(this)
        {
            Text =
                "Battery optimisation is what stops this device reporting overnight. Android " +
                "and several manufacturers' own power managers will freeze a background app " +
                "after a few hours idle; the device then reads offline in the console until " +
                "somebody picks it up. Excluding the agent is the fix, and it has to be granted " +
                "here -- an app cannot grant it to itself.\n\n" +
                "Location is what the console's Locate button needs. A fully managed device " +
                "grants it to itself the first time somebody asks where it is; every other " +
                "device has to be granted it here, by somebody holding it.",
        };
        hint.SetPadding(0, 32, 0, 0);
        root.AddView(hint);

        var scroll = new ScrollView(this);
        scroll.AddView(root);
        return scroll;
    }

    private TextView Label(string text)
    {
        var label = new TextView(this) { Text = text };
        label.SetPadding(0, 24, 0, 0);
        return label;
    }

    // ------------------------------------------------------------------ actions

    private void Save()
    {
        var hub = _hubField.Text?.Trim() ?? "";
        if (hub.Length > 0)
        {
            // Validated the same way the agent validates it, so a typo is refused here rather
            // than leaving a device that looks configured and reports nowhere.
            var before = AgentConfig.HubBase;
            AgentConfig.Configure(hub);
            if (AgentConfig.HubBase == before && hub.TrimEnd('/') != before)
            {
                Toast.MakeText(this, "That hub URL is not usable -- it needs to start with https://",
                    ToastLength.Long)?.Show();
                return;
            }
            _state.SaveHubBaseOverride(AgentConfig.HubBase);
        }

        var secret = _secretField.Text?.Trim() ?? "";
        if (secret.Length > 0)
        {
            if (!_state.SaveEnrollmentSecret(secret))
            {
                Toast.MakeText(this, "The secret could not be saved on this device",
                    ToastLength.Long)?.Show();
                return;
            }
            // Cleared from the field once stored. It is a fleet-wide credential and leaving it
            // on screen behind an unlocked phone is the easiest way for it to travel.
            _secretField.Text = "";
        }

        // The service re-reads both on its next enrollment attempt -- see
        // AndroidEnrollmentSecretSource, which is asked afresh every time for exactly this
        // reason. Restarting the service is still the fastest way to pick up a changed HUB,
        // which is read once at composition.
        AgentService.Start(this);

        Toast.MakeText(this, "Saved. Enrolment is retried every 30 seconds.", ToastLength.Long)?.Show();
        RefreshStatus();
    }

    /// <summary>Open the system's battery optimisation screen.
    ///
    /// The LIST, not the "allow this app to ignore optimisation?" dialog. That dialog needs the
    /// REQUEST_IGNORE_BATTERY_OPTIMIZATIONS permission, which Play policy treats as a
    /// violation for most apps and which several OEMs ignore anyway; the list is available on
    /// every device and puts the person in the screen their own manufacturer respects. Falls
    /// back to the app's settings page on a device that has neither.</summary>
    private void OpenBatterySettings()
    {
        var candidates = new[]
        {
            global::Android.Provider.Settings.ActionIgnoreBatteryOptimizationSettings,
            global::Android.Provider.Settings.ActionApplicationDetailsSettings,
        };

        foreach (var action in candidates)
        {
            try
            {
                var intent = new Intent(action);
                if (action == global::Android.Provider.Settings.ActionApplicationDetailsSettings)
                    intent.SetData(global::Android.Net.Uri.Parse("package:" + PackageName));
                StartActivity(intent);
                return;
            }
            catch { /* try the next one */ }
        }

        Toast.MakeText(this, "No battery settings screen on this device", ToastLength.Long)?.Show();
    }

    /// <summary>Open the usage-access screen -- roadmap #23 phase E.
    ///
    /// **The one permission a Device Owner cannot grant for you.** Usage access is an appop,
    /// not a dangerous runtime permission, so setPermissionGrantState does not reach it and no
    /// provisioning flow turns it on. Without it every screen-time BUDGET reads zero minutes
    /// used and therefore never fires -- which looks, from the console, exactly like a device
    /// nobody has been using. Curfews are unaffected; they need a clock, not a ledger.
    ///
    /// The list screen is opened rather than this app's row in it, because the per-app deep
    /// link is not public API and several OEMs do not honour it. Falls back to the app's own
    /// settings page, which is where a technician can reach the same switch by hand.</summary>
    private void OpenUsageAccessSettings()
    {
        var candidates = new[]
        {
            global::Android.Provider.Settings.ActionUsageAccessSettings,
            global::Android.Provider.Settings.ActionApplicationDetailsSettings,
        };

        foreach (var action in candidates)
        {
            try
            {
                var intent = new Intent(action);
                if (action == global::Android.Provider.Settings.ActionApplicationDetailsSettings)
                    intent.SetData(global::Android.Net.Uri.Parse("package:" + PackageName));
                StartActivity(intent);
                return;
            }
            catch { /* try the next one */ }
        }

        Toast.MakeText(this, "No usage access screen on this device", ToastLength.Long)?.Show();
    }

    /// <summary>Get this device the location permission -- roadmap #23 phase B.
    ///
    /// **Not shaped like the two buttons above it, because location is not that kind of
    /// permission.** Battery optimisation and usage access are special-access switches an app
    /// can only point somebody at; location is an ordinary runtime permission, so on an
    /// unmanaged device the platform puts the question directly in front of the person holding
    /// it, and on a managed one the Device Owner answers it without a dialog at all. Both paths
    /// end in the same place and this button takes whichever one this device has.
    ///
    /// **Both permissions are asked for together, because asking for FINE alone is a request
    /// Android 12 and later may ignore.** From API 31 approximate location is its own grant
    /// with its own button in the system dialog, and the pair is the documented way to reach
    /// either. AndroidLocationReader accepts whichever comes back.
    ///
    /// **The second refusal is final, so there is a fallback.** Once somebody has said no
    /// twice, requestPermissions returns immediately with no dialog -- a button that silently
    /// does nothing, on the one screen whose whole job is to make invisible states visible. See
    /// OnRequestPermissionsResult, which sends them to the app's own settings page instead.
    ///
    /// **Not asked automatically on launch, unlike the notification permission**, and the
    /// difference is what a refusal costs. A reflexive "Don't allow" on notifications loses a
    /// status display; on location it loses the feature, and it is close to irreversible
    /// without a walk through Settings. A technician who presses this button has read the line
    /// above it and decided. A device where nobody pressed it answers a locate with
    /// "unavailable" and the reason, which is the state this screen now shows.</summary>
    private void GrantLocation()
    {
        if (AndroidLocationReader.IsGranted(this))
        {
            Toast.MakeText(this, "Location is already granted on this device", ToastLength.Short)?.Show();
            return;
        }

        // A fully managed device does not need the dialog and must not be sent to Settings:
        // it can simply take the permission. Tried first so a technician provisioning a phone
        // by QR sees the status line flip to granted while they are still holding it, rather
        // than finding out at the first locate weeks later.
        if (DeviceOwner.GrantLocation(this, Logger()) && AndroidLocationReader.IsGranted(this))
        {
            Toast.MakeText(this, "Location granted -- this device can be located",
                ToastLength.Long)?.Show();
            RefreshStatus();
            return;
        }

        RequestPermissions(
            [
                global::Android.Manifest.Permission.AccessFineLocation,
                global::Android.Manifest.Permission.AccessCoarseLocation,
            ],
            RequestLocation);
    }

    /// <summary>What the system said to a runtime permission request.
    ///
    /// Only location is handled. The notification permission is asked on every launch and
    /// nothing on this screen depends on the answer, so it has nothing to report; this one is
    /// asked by a person pressing a button and expecting the screen to change.</summary>
    public override void OnRequestPermissionsResult(
        int requestCode, string[] permissions, Permission[] grantResults)
    {
        base.OnRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode != RequestLocation) return;

        RefreshStatus();
        if (AndroidLocationReader.IsGranted(this)) return;

        // ShouldShowRequestPermissionRationale is false both before the first ask and after the
        // last one, which is normally the trap in this API. Here it cannot be: the request that
        // just returned WAS an ask, so false at this point means the only one it can mean --
        // the platform will not raise the dialog again, and the switch has moved to Settings.
        var permanent = !ShouldShowRequestPermissionRationale(
            global::Android.Manifest.Permission.AccessFineLocation);
        if (!permanent)
        {
            Toast.MakeText(this, "Location was not granted. This device cannot be located.",
                ToastLength.Long)?.Show();
            return;
        }

        Toast.MakeText(this, "Android will not ask again. Turn Location on for FleetHub Agent "
                             + "under Permissions.", ToastLength.Long)?.Show();
        OpenAppSettings();
    }

    /// <summary>This app's own row in Settings. Shared by the paths above, each of which has
    /// already tried the thing that would have been better and been refused.</summary>
    private void OpenAppSettings()
    {
        try
        {
            var intent = new Intent(
                global::Android.Provider.Settings.ActionApplicationDetailsSettings);
            intent.SetData(global::Android.Net.Uri.Parse("package:" + PackageName));
            StartActivity(intent);
        }
        catch (Exception)
        {
            Toast.MakeText(this, "No app settings screen on this device", ToastLength.Long)?.Show();
        }
    }

    /// <summary>A logger for the one call on this screen that takes one. The service builds its
    /// own factory and this activity has no business sharing it -- a logger held across the two
    /// would outlive whichever of them stopped first.</summary>
    private static ILogger Logger() =>
        new LogcatLoggerProvider(LogLevel.Information).CreateLogger("DeviceOwner");

    private void RequestNotificationPermissionIfNeeded()
    {
        // OperatingSystem.IsAndroidVersionAtLeast rather than a Build.VERSION.SdkInt
        // comparison: both are correct at runtime, but only this one is understood by the
        // platform-compatibility analyzer, which otherwise reports CA1416 on the
        // API-33-only constant below. See the note on AgentService.StartInForeground.
        if (!OperatingSystem.IsAndroidVersionAtLeast(33)) return;
        if (CheckSelfPermission(global::Android.Manifest.Permission.PostNotifications) == Permission.Granted)
            return;

        RequestPermissions([global::Android.Manifest.Permission.PostNotifications], RequestPostNotifications);
    }

    private void RefreshStatus()
    {
        var identity = _state.LoadIdentity();
        var name = _state.LoadMachineNameOverride();
        var hasSecret = new AndroidEnrollmentSecretSource(this, _state).Read() is not null;

        var lines = new List<string>
        {
            $"FleetHub Agent {AgentConfig.Version}",
            // Not "Hub: https://your.hub.url". That line is read by somebody deciding whether
            // this device is finished, and a placeholder URL formatted exactly like a real one
            // reads as configured at a glance. This is the state a QR-provisioned device was
            // silently left in, so it is now the loudest line on the screen.
            AgentConfig.IsHubConfigured
                ? $"Hub: {AgentConfig.HubBase}"
                : "NO HUB CONFIGURED -- this device is reporting NOWHERE. Enter the hub URL "
                  + "below, or provision it with a QR code minted by the console.",
            $"Machine: {name ?? "(derived from this device)"}",
            // Whether the QR provisioning actually took, and whether the one switch nobody can
            // flip remotely has been flipped. Both are invisible otherwise, and both are things
            // a technician holding the device can fix in the next thirty seconds.
            DeviceOwner.Describe(this),
            UsageStatsReader.IsGranted(this)
                ? "Usage access granted -- screen-time budgets can be enforced."
                : "Usage access NOT granted. Blocked hours still work; screen-time budgets " +
                  "will never fire, because this device cannot see how long an app was used.",
            // The permission the console's Locate button rests on, and one nothing used to
            // ask for. Stated here for the same reason as the line above: it is granted per
            // device, by a person, and its absence is otherwise visible only to whoever
            // presses Locate and reads the reason that comes back.
            AndroidLocationReader.IsGranted(this)
                ? "Location granted -- this device can be located from the console."
                : "Location NOT granted. A locate from the console will answer \"unavailable\" " +
                  "instead of a position. Use the Grant location button below.",
        };

        if (identity.IsEnrolled)
        {
            lines.Add($"Enrolled as {identity.AgentId}");
        }
        else if (hasSecret)
        {
            lines.Add("Not enrolled yet -- retrying every 30 seconds.");
        }
        else
        {
            // The state that looks fine and is not. Said plainly, because on a phone there is
            // no installer output to say it in.
            lines.Add(
                "NOT ENROLLED, and no enrollment secret is set. This device will appear in " +
                "the console, chart its temperature and report its inventory -- and will " +
                "never accept a command.");
        }

        _status.Text = string.Join("\n\n", lines);
    }
}
