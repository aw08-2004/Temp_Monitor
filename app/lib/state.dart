/// Riverpod wiring: session, polling, and the alert-delta that raises notifications
/// (roadmap #11).
///
/// **Polling, not push.** Every ten seconds the app re-reads the roster and the alert
/// list. The hub's Socket.IO is polling-only, CORS-pinned to its own origin and costs a
/// server thread per open connection, so subscribing would mean widening the hub's
/// perimeter to save nothing an operator would notice -- the console's own Inventory page
/// refreshes on a thirty-second timer.
///
/// **Notifications are derived here, not sent.** The app diffs each poll against the
/// alerts it has already shown and toasts what is new. v1 needs no push service at all:
/// a desktop app that is running holds its own connection, and one that is not running
/// has nothing to notify. FCM/APNs arrive with the phone build, against the same
/// `alert.id` identity used below.
library;

import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'api/fleet_api.dart';
import 'api/hub_client.dart';
import 'auth/token_store.dart';
import 'models.dart';
import 'notify.dart';

/// How often the fleet and the alert list are re-read. Ten seconds is a compromise: the
/// hub marks a machine offline after ninety, so this is well inside the resolution of the
/// thing being watched, and it is one small request per tick.
const Duration pollInterval = Duration(seconds: 10);

final tokenStoreProvider = Provider<TokenStore>((ref) => TokenStore());

final notifierProvider = Provider<AlertNotifier>((ref) => AlertNotifier());

/// The current session, or null when this device is not paired. Everything else in the
/// app hangs off this: there is exactly one place that answers "are we signed in".
class SessionController extends StateNotifier<AsyncValue<StoredSession?>> {
  SessionController(this._store, this._notifier)
      : super(const AsyncValue.loading()) {
    _load();
  }

  final TokenStore _store;

  /// Held only so [forget] can reset it. The notifier is a plain Provider and therefore
  /// outlives any one pairing, which is the whole reason this dependency exists.
  final AlertNotifier _notifier;

  Future<void> _load() async {
    try {
      state = AsyncValue.data(await _store.read());
    } catch (e, st) {
      state = AsyncValue.error(e, st);
    }
  }

  Future<void> adopt(StoredSession session) async {
    await _store.write(session);
    state = AsyncValue.data(session);
  }

  /// Forget this device locally. The caller should have already revoked it server-side --
  /// see FleetApi.revokeSelf -- because a token that still works but is on no screen is
  /// the worst of both.
  Future<void> forget() async {
    await _store.clear();
    // Drop the alert-delta with the session. `notifierProvider` has no dependencies, so
    // its AlertNotifier is built once for the PROCESS and survives sign-out: without this
    // the next operator to pair on this machine starts against the previous operator's
    // seen-set with priming already spent, and their first poll toasts every alert that
    // operator had not seen. Two people on one helpdesk PC rarely have the same machine
    // scope, so that is not a small set -- it is the launch-notification storm `_priming`
    // exists to prevent, arriving at sign-in instead of at startup.
    //
    // Here rather than in the sign-out button because this is the one seam both ways out
    // of a session pass through: the button, and a poll that takes an
    // UnauthenticatedException because the token was revoked underneath it.
    _notifier.reset();
    state = const AsyncValue.data(null);
  }
}

final sessionProvider =
    StateNotifierProvider<SessionController, AsyncValue<StoredSession?>>((ref) =>
        SessionController(
            ref.watch(tokenStoreProvider), ref.watch(notifierProvider)));

/// The API, rebuilt whenever the session changes and disposed with it. Null until paired.
final apiProvider = Provider<FleetApi?>((ref) {
  final session = ref.watch(sessionProvider).valueOrNull;
  if (session == null) return null;
  final client = HubClient(baseUrl: session.hubUrl, token: session.token);
  ref.onDispose(client.close);
  return FleetApi(client);
});

/// Polls `fetch` on [pollInterval] and republishes the result.
///
/// A poll that throws [UnauthenticatedException] does not merely surface an error: it
/// ends the session, because the token has been revoked or its owner has lost access and
/// no amount of retrying will change that. Every other failure keeps the LAST GOOD value
/// on screen with the error beside it -- a dropped Wi-Fi should not blank the fleet list.
class Poller<T> extends StateNotifier<AsyncValue<T>> {
  Poller(this._ref, this._fetch) : super(const AsyncValue.loading()) {
    _tick();
    _timer = Timer.periodic(pollInterval, (_) => _tick());
  }

  final Ref _ref;
  final Future<T> Function() _fetch;
  Timer? _timer;

  Future<void> refresh() => _tick();

  Future<void> _tick() async {
    try {
      final value = await _fetch();
      if (mounted) state = AsyncValue.data(value);
    } on UnauthenticatedException {
      await _ref.read(sessionProvider.notifier).forget();
    } catch (e, st) {
      if (!mounted) return;
      final previous = state.valueOrNull;
      // The new state IS the error; the previous value rides along so AsyncBody can keep
      // showing the last good roster with the failure beside it. A dropped Wi-Fi must not
      // blank a fleet list somebody is reading.
      state = previous == null
          ? AsyncValue.error(e, st)
          : AsyncValue<T>.error(e, st)
              .copyWithPrevious(AsyncValue<T>.data(previous));
    }
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }
}

final machinesProvider =
    StateNotifierProvider<Poller<List<Machine>>, AsyncValue<List<Machine>>>(
        (ref) {
  final api = ref.watch(apiProvider);
  return Poller(ref, () async => api == null ? <Machine>[] : api.machines());
});

final alertsProvider =
    StateNotifierProvider<Poller<List<Alert>>, AsyncValue<List<Alert>>>((ref) {
  final api = ref.watch(apiProvider);
  final notifier = ref.watch(notifierProvider);
  return Poller(ref, () async {
    if (api == null) return <Alert>[];
    final alerts = await api.alerts();
    // The delta, not the list: an alert already on screen must not toast again on the
    // next tick, which at ten seconds would be six notifications a minute, forever.
    await notifier.notifyNew(alerts);
    return alerts;
  });
});

final wakeRequestsProvider = StateNotifierProvider<Poller<List<WakeRequest>>,
    AsyncValue<List<WakeRequest>>>((ref) {
  final api = ref.watch(apiProvider);
  return Poller(
      ref, () async => api == null ? <WakeRequest>[] : api.wakeRequests());
});

/// Fetched once per session rather than polled: capabilities change when an admin
/// changes them, which is rare, and the server-side gate is the actual control -- this
/// only decides which buttons are drawn.
final capabilitiesProvider = FutureProvider<DeviceCapabilities?>((ref) async {
  final api = ref.watch(apiProvider);
  return api?.me();
});

final favoritesProvider = FutureProvider<List<Favorite>>((ref) async {
  final api = ref.watch(apiProvider);
  return api == null ? <Favorite>[] : api.favorites();
});
