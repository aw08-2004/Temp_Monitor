/// Every user-facing string in the app, in one place (roadmap #11).
///
/// **v1 is English only, and that is a decision rather than an omission.** What v1 buys
/// is that no string is written inline in a widget: adding languages later is then an
/// asset drop plus a lookup swap, instead of a sweep through every screen -- which is the
/// expensive half and the half that gets skipped.
///
/// **v2 ships the catalogs INSIDE the app**, generated from the hub's own
/// `hub/locales/{en,de,es}.json`, switchable at runtime from the app's settings. Bundled
/// rather than fetched, because this app is opened in a car park on a phone with no
/// signal, and a UI whose labels depend on a round-trip is a UI that is sometimes blank.
/// The cost is drift from the hub's catalogs; the answer is generating the assets in the
/// build rather than hand-copying them, plus a test asserting the two key sets agree --
/// the same shape as the hub's own tests/test_i18n.py.
///
/// Until then this class is deliberately a plain map lookup with the same SHAPE a
/// generated localisations class has, so the swap is mechanical.
library;

class S {
  const S();

  // ---- chrome
  String get appTitle => 'FleetHub';
  String get fleet => 'Fleet';
  String get alerts => 'Alerts';
  String get wake => 'Wake';
  String get commands => 'Commands';
  String get settings => 'Settings';

  // ---- pairing
  String get pairTitle => 'Connect to your hub';
  String get pairIntro =>
      'Sign in through your browser to give this device access. You can revoke it at '
      'any time from the hub.';
  String get hubAddress => 'Hub address';
  String get hubAddressHint => 'fleethub.example.com';
  String get deviceName => 'Device name';
  String get deviceNameHint => 'This PC';
  String get pairButton => 'Sign in with your browser';
  String get pairWaiting => 'Waiting for your browser…';
  String get pairManual => 'Pair with a code instead';
  String get pairingCode => 'Pairing code';
  String get pairCodeButton => 'Finish pairing';

  // ---- fleet
  String get searchMachines => 'Search machines';
  String get noMachines => 'No machines are in view.';
  String get noMachinesHint =>
      'This device sees the machines your account can see. If that is none, ask an '
      'administrator about your permission group.';
  String get online => 'Online';
  String get offline => 'Offline';
  String get lastSeen => 'Last seen';
  String get never => 'Never';
  String get model => 'Model';
  String get serial => 'Serial';
  String get assetTag => 'Asset tag';
  String get serviceTag => 'Service tag';
  String get uptime => 'Uptime';
  String get temperature => 'Temperature';
  String get runningHot => 'Running hot';

  // ---- alerts
  String get noAlerts => 'Nothing is wrong.';
  String get noAlertsHint => 'Open alerts appear here, and as a notification.';
  String get dismiss => 'Dismiss';

  // ---- wake
  String get wakeMachine => 'Wake';
  String get wakeHistory => 'Recent wakes';
  String get noWakes => 'No wake attempts yet.';
  // Every one of these is an ATTEMPT, and the wording says so: nothing acknowledges a
  // magic packet, so "sent" is never rendered as success. The hub draws the same line.
  String wakeStatus(String status) => switch (status) {
        'pending' => 'Looking for a relay',
        'relaying' => 'Asking a nearby PC to send it',
        'sent' => 'Packet sent — waiting for the PC',
        'awake' => 'Checked in — awake',
        'already_awake' => 'Was already on',
        'no_relay' => 'No awake PC on its subnet to relay through',
        'no_answer' => 'Packet sent, no answer',
        'unwakeable' => 'This PC cannot be woken',
        'cancelled' => 'Cancelled',
        _ => status,
      };

  // ---- commands
  String get noFavorites => 'No saved commands.';
  String get noFavoritesHint =>
      'Saved commands are created in the console and appear here as quick actions.';
  String get pickMachine => 'Pick a machine';
  String commandQueued(String machine) => 'Queued for $machine.';

  // ---- settings
  String get signedInAs => 'Signed in as';
  String get connectedTo => 'Connected to';
  String get thisDeviceMay => 'This device may';
  String get signOut => 'Sign out';
  String get signOutHelp =>
      'Revokes this device on the hub as well as forgetting it here.';
  String get notPermitted => 'This device was not given permission to do that.';

  // ---- shared
  String get retry => 'Retry';
  String get cancel => 'Cancel';
  String get refresh => 'Refresh';
}

const S strings = S();
