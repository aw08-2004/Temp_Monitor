// The alert-delta that decides when a toast is raised (roadmap #11).
//
// This logic is small and gets every detail wrong by default, so it is pinned rather than
// eyeballed: notifying at launch trains people to dismiss FleetHub toasts unread, and
// re-notifying an alert that is merely still open would fire six times a minute forever.
//
// AlertNotifier.notifyNew calls into local_notifier to show a toast, which does nothing
// in a test binding -- and _toast swallows its own failures precisely so a notification
// that cannot be shown never breaks the poll that produced it. What is asserted here is
// the SET arithmetic that decides what gets that far.

import 'package:flutter_test/flutter_test.dart';
import 'package:fleethub_client/models.dart';
import 'package:fleethub_client/notify.dart';

Alert alert(String id, {String machine = 'PC-1'}) =>
    Alert(id: id, kind: 'high_temp', machine: machine, message: 'hot');

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('the first poll primes and never notifies', () async {
    final notifier = AlertNotifier();
    expect(notifier.isPriming, isTrue);
    expect(await notifier.notifyNew([alert('a'), alert('b')]), isEmpty,
        reason: 'alerts that existed at launch are history, not news');
    expect(notifier.isPriming, isFalse);
  });

  test('an alert that is still open does not notify again', () async {
    final notifier = AlertNotifier();
    await notifier.notifyNew([alert('a')]); // prime
    expect(await notifier.notifyNew([alert('a')]), isEmpty);
    expect(await notifier.notifyNew([alert('a')]), isEmpty,
        reason: 'still open is not the same as newly raised -- at a ten-second '
            'poll this would be six toasts a minute, forever');
    expect((await notifier.notifyNew([alert('a'), alert('b')])).map((a) => a.id),
        ['b']);
  });

  test('a dismissed-then-re-raised alert is news again', () async {
    final notifier = AlertNotifier();
    await notifier.notifyNew([alert('a')]); // prime
    await notifier.notifyNew(const <Alert>[]); // dismissed -- forget it
    // Re-raised. If the seen-set never shrank, a machine that goes hot, is dismissed and
    // goes hot again would notify only the first time, which is the failure that makes
    // the feature useless on the machine that keeps overheating.
    expect((await notifier.notifyNew([alert('a')])).map((a) => a.id), ['a']);
  });

  test('reset returns it to priming, so a new operator inherits nothing',
      () async {
    final notifier = AlertNotifier();
    await notifier.notifyNew([alert('a')]);
    expect(notifier.isPriming, isFalse);
    notifier.reset();
    expect(notifier.isPriming, isTrue);
  });
}
