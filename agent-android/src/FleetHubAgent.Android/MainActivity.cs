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

        var hint = new TextView(this)
        {
            Text =
                "Battery optimisation is what stops this device reporting overnight. Android " +
                "and several manufacturers' own power managers will freeze a background app " +
                "after a few hours idle; the device then reads offline in the console until " +
                "somebody picks it up. Excluding the agent is the fix, and it has to be granted " +
                "here -- an app cannot grant it to itself.",
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
