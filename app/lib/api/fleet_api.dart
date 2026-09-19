/// Typed calls over the hub endpoints the app uses (roadmap #11).
///
/// **Every endpoint named here already exists** and is already scope-filtered and tested
/// on the hub side. That is the design: v1 adds authentication to the hub and nothing
/// else, and a screen that needs data the console does not already serve is a screen
/// outside v1's scope rather than a reason for a new route.
library;

import '../models.dart';
import 'hub_client.dart';

class FleetApi {
  const FleetApi(this.client);

  final HubClient client;

  // ---------------------------------------------------------------- fleet
  /// The roster. Already narrowed to this operator's machine scope by the hub, so there
  /// is nothing to filter here -- and nothing the app could filter correctly if there
  /// were.
  Future<List<Machine>> machines() async {
    final body = await client.get('/api/machines');
    return rowsOf(body, 'machines')
        .map(Machine.fromJson)
        .toList(growable: false);
  }

  Future<Machine> machine(String name) async {
    final body = await client.get('/api/machines/${Uri.encodeComponent(name)}');
    return Machine.fromJson(body is Map<String, dynamic> ? body : const {});
  }

  /// One machine's live sensors, as the machine page's cards read them.
  Future<List<Map<String, dynamic>>> sensors(String name) async {
    final body =
        await client.get('/api/machines/${Uri.encodeComponent(name)}/sensors');
    return rowsOf(body, 'sensors');
  }

  // ---------------------------------------------------------------- alerts
  Future<List<Alert>> alerts() async {
    final body = await client.get('/api/alerts');
    return rowsOf(body, 'alerts').map(Alert.fromJson).toList(growable: false);
  }

  Future<void> dismissAlert(String id) =>
      client.post('/api/alerts/${Uri.encodeComponent(id)}/dismiss');

  // ---------------------------------------------------------------- wake
  /// Wake one machine. The hub answers 202 for every non-error outcome INCLUDING
  /// `no_relay` and `unwakeable`, because those are facts about the fleet rather than
  /// faults in the call -- so this returns the request rather than a bool, and the UI
  /// reports the state it actually reached.
  Future<WakeRequest> wake(String machine, {String reason = ''}) async {
    final body = await client.post(
      '/api/wake/machines/${Uri.encodeComponent(machine)}',
      body: {'reason': reason},
    );
    final request = (body is Map && body['request'] is Map<String, dynamic>)
        ? body['request'] as Map<String, dynamic>
        : (body is Map<String, dynamic> ? body : const <String, dynamic>{});
    return WakeRequest.fromJson(request);
  }

  Future<List<WakeRequest>> wakeRequests({bool openOnly = false}) async {
    final body = await client.get('/api/wake/requests',
        query: openOnly ? {'open': '1'} : null);
    return rowsOf(body, 'requests')
        .map(WakeRequest.fromJson)
        .toList(growable: false);
  }

  Future<void> cancelWake(String id) =>
      client.post('/api/wake/requests/${Uri.encodeComponent(id)}/cancel');

  // ---------------------------------------------------------------- commands
  Future<List<Favorite>> favorites() async {
    final body = await client.get('/api/fleet/favorites');
    return rowsOf(body, 'favorites')
        .map(Favorite.fromJson)
        .toList(growable: false);
  }

  /// Issue a command. Returns the hub's command id so the caller can follow it; the app
  /// does not wait, because a machine that is asleep will answer whenever it wakes and a
  /// spinner that never ends is worse than a queued row.
  Future<String> issueCommand(String machine, String type,
      {Map<String, dynamic> params = const {}}) async {
    final body = await client.post('/api/fleet/commands', body: {
      'machine': machine,
      'type': type,
      'params': params,
    });
    return (body is Map && body['id'] != null) ? body['id'].toString() : '';
  }

  Future<Map<String, dynamic>> command(String id) async {
    final body =
        await client.get('/api/fleet/commands/${Uri.encodeComponent(id)}');
    return body is Map<String, dynamic> ? body : const {};
  }

  // ---------------------------------------------------------------- this device
  Future<DeviceCapabilities> me() async {
    final body = await client.get('/api/permissions/me');
    return DeviceCapabilities.fromJson(
        body is Map<String, dynamic> ? body : const {});
  }

  /// Revoke this device server-side. Signing out has to do this, not merely forget the
  /// token locally: a token that still works but is no longer on any screen is the worst
  /// of both -- nobody can see it, and it can still act.
  Future<void> revokeSelf(String tokenId) =>
      client.delete('/api/tokens/${Uri.encodeComponent(tokenId)}');

  // ---------------------------------------------------------------- push
  /// Whether this hub can push at all, and whether this device is registered.
  ///
  /// **Ask this BEFORE asking the operating system for a notification permission.** A
  /// phone that prompts and then never notifies teaches its owner that the prompt was
  /// pointless, and the reason -- the hub has no Firebase credentials -- is one only the
  /// hub can report. `configured` is about the hub and `registered` about this device;
  /// they fail separately and are reported separately.
  Future<PushStatus> pushStatus() async {
    final body = await client.get('/api/push/status');
    return PushStatus.fromJson(body is Map<String, dynamic> ? body : const {});
  }

  /// Register this install's FCM (or, one day, APNs) token with the hub.
  ///
  /// Called on EVERY launch rather than once at pairing, because FCM issues a new
  /// registration token whenever the app is reinstalled, its data is cleared, or it is
  /// restored onto a new phone -- and a registration the hub still believes in is a
  /// registration that pushes into the void. The hub upserts, and deliberately leaves
  /// this device's alert cursor alone, so re-registering never delivers a backlog.
  ///
  /// The device is never named in the body: the hub reads it off the bearer token, which
  /// is what makes it impossible to register a push address against somebody else's
  /// device.
  Future<void> registerPush({required String kind, required String token}) =>
      client.post('/api/push/register', body: {'kind': kind, 'token': token});

  /// Stop pushing to this device. Not a sign-out -- the token stays valid and the desktop
  /// half keeps polling.
  Future<void> unregisterPush() => client.delete('/api/push/register');

  /// Tell the hub the alert list has actually been LOOKED AT on this device.
  ///
  /// Delivery is not readership. The hub counts unseen alerts against a per-device cursor
  /// that only this call moves forward, so skipping it means the next notification says
  /// "1" when the operator has four waiting -- the count being wrong at exactly the moment
  /// the count is the entire message.
  Future<void> pushSeen() => client.post('/api/push/seen');
}
