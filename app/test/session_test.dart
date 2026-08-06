// Signing out has to reset the alert-delta, and this pins the WIRING rather than the unit
// (roadmap #11).
//
// notify_test.dart already asserts that AlertNotifier.reset() returns it to priming, and
// that test passed for the entire time nothing called reset() in production -- which is
// the failure mode worth a file of its own. AlertNotifier is built by a Provider with no
// dependencies, so one instance serves the whole process and outlives every pairing. The
// unit was right; the seam between it and SessionController.forget was missing, and a test
// that exercises a class directly cannot see that.
//
// Reading sessionProvider does not touch apiProvider, so nothing here makes a request.

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:fleethub_client/auth/token_store.dart';
import 'package:fleethub_client/models.dart';
import 'package:fleethub_client/state.dart';

/// An in-memory TokenStore. Subclassed rather than mocked so the real constructor still
/// runs -- it only builds a FlutterSecureStorage value object, and reaching a platform
/// channel is what the overrides below prevent.
class _FakeStore extends TokenStore {
  StoredSession? _session;

  @override
  Future<StoredSession?> read() async => _session;

  @override
  Future<void> write(StoredSession session) async => _session = session;

  @override
  Future<void> clear() async => _session = null;
}

const _session = StoredSession(
  hubUrl: 'https://hub.example',
  token: 'tmu_abc:secret',
  tokenId: 'abc',
  email: 'operator@example.com',
);

Alert _alert(String id) =>
    Alert(id: id, kind: 'high_temp', machine: 'PC-$id', message: 'hot');

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  Future<ProviderContainer> paired() async {
    final container = ProviderContainer(
      overrides: [tokenStoreProvider.overrideWithValue(_FakeStore())],
    );
    addTearDown(container.dispose);
    // Build the controller and let its initial read settle before adopting.
    container.read(sessionProvider.notifier);
    await Future<void>.delayed(Duration.zero);
    await container.read(sessionProvider.notifier).adopt(_session);
    return container;
  }

  test('signing out returns the alert notifier to priming', () async {
    final container = await paired();
    final notifier = container.read(notifierProvider);

    await notifier.notifyNew([_alert('a')]);
    expect(notifier.isPriming, isFalse, reason: 'the first poll primes');

    await container.read(sessionProvider.notifier).forget();

    expect(notifier.isPriming, isTrue,
        reason: 'a new operator on this machine must inherit nothing from the '
            'previous one -- otherwise their first poll toasts every alert the '
            'previous operator had not already seen');
  });

  test('the operator who pairs next is not toasted the previous one\'s alerts',
      () async {
    final container = await paired();
    final notifier = container.read(notifierProvider);

    // One operator, with one alert already seen.
    await notifier.notifyNew([_alert('a')]);
    await container.read(sessionProvider.notifier).forget();

    // The next operator's scope shows alerts the first one never had. This is the first
    // poll of a NEW session, so it must prime and raise nothing. Without the reset it
    // raises both, because neither id is in the previous operator's seen-set -- and two
    // is the small end of that mistake on a helpdesk PC where the scopes barely overlap.
    final atSignIn = await notifier.notifyNew([_alert('b'), _alert('c')]);
    expect(atSignIn, isEmpty,
        reason: 'the alerts already open when a session starts are history');
    expect(notifier.isPriming, isFalse);

    // And from here the delta behaves normally again: 'd' is genuinely news.
    final later =
        await notifier.notifyNew([_alert('b'), _alert('c'), _alert('d')]);
    expect(later.map((a) => a.id), ['d']);
  });

  test('forgetting a session clears the stored credential too', () async {
    final container = await paired();
    expect(container.read(sessionProvider).valueOrNull?.token, _session.token);

    await container.read(sessionProvider.notifier).forget();

    expect(container.read(sessionProvider).valueOrNull, isNull);
  });
}
