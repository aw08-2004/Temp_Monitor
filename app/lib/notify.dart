/// Windows toasts for new alerts, and the tray icon that makes them worth having
/// (roadmap #11).
///
/// **v1 raises notifications client-side and needs no push service.** The app polls the
/// alert list, diffs it against what it has already shown, and toasts what is new. There
/// is no FCM project, no device-token registration and no server-side sender to test
/// against nothing -- a running desktop app holds its own connection, and one that is not
/// running has nothing to notify. Phase 2's phone build is where push earns its
/// complexity, and it will key off the same `alert.id` used below.
///
/// **Windows will not show a toast for an application with no identity.** `local_notifier`
/// registers a Start Menu shortcut carrying an AppUserModelID, which is what gives an
/// unsigned, non-MSIX build one. If toasts silently do nothing, that shortcut is the first
/// thing to check -- not the notification code.
library;

import 'package:local_notifier/local_notifier.dart';

import 'models.dart';

class AlertNotifier {
  AlertNotifier();

  /// Alert ids already toasted. Ids rather than a count, because alerts are dismissed and
  /// raised independently: counting would re-notify the whole list every time one was
  /// dismissed, which is the moment an operator is least interested in being interrupted.
  final Set<String> _seen = <String>{};

  /// True until the first poll has been folded in. The alerts that exist when the app
  /// STARTS are history, not news -- toasting nine of them at launch trains people to
  /// dismiss FleetHub notifications without reading them, which costs the feature its
  /// entire value.
  bool _priming = true;

  bool get isPriming => _priming;

  /// Fold in one poll's worth of alerts, toasting anything genuinely new.
  Future<void> notifyNew(List<Alert> alerts) async {
    final current = {for (final alert in alerts) alert.id: alert};

    if (_priming) {
      _priming = false;
      _seen.addAll(current.keys);
      return;
    }

    final fresh = current.keys.where((id) => !_seen.contains(id)).toList();
    // Forget ids that are no longer open, so a re-raised alert on the same machine is
    // news again. Without this the set grows for the life of the process and a machine
    // that goes hot, is dismissed, and goes hot again would notify only once.
    _seen
      ..removeWhere((id) => !current.containsKey(id))
      ..addAll(current.keys);

    for (final id in fresh) {
      await _toast(current[id]!);
    }
  }

  Future<void> _toast(Alert alert) async {
    final notification = LocalNotification(
      title: alert.machine.isEmpty ? 'FleetHub alert' : alert.machine,
      body: alert.message.isEmpty ? alert.kind : alert.message,
    );
    try {
      await notification.show();
    } catch (_) {
      // A toast that cannot be shown must never take down the poll that produced it.
      // Losing a notification is a nuisance; losing the fleet list is the app.
    }
  }

  /// Used by the tests, and by "sign out" -- a new operator on the same machine should
  /// not inherit the previous one's idea of what has already been seen.
  void reset() {
    _seen.clear();
    _priming = true;
  }
}
