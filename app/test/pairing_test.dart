// The pairing flow, against a fake hub (roadmap #11).
//
// This is where the security of the app lives, so the REJECTION cases are the point:
// pairing hands a device a months-long credential to a fleet-management console, and the
// browser round-trip is the only part of it an attacker can reach.
//
// The loopback listener is driven directly rather than through a real browser: `open` is
// injectable precisely so a test can be the browser, follow the redirect, and — more
// usefully — follow the wrong one.

import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:fleethub_client/auth/pairing.dart';

/// A hub that hands out one code and exchanges it once, like the real one.
class FakeHub {
  FakeHub._(this._server);

  final HttpServer _server;
  String issuedCode = 'the-one-time-code';
  bool codeUsed = false;
  int exchangeCalls = 0;

  String get url => 'http://127.0.0.1:${_server.port}';

  static Future<FakeHub> start() async {
    final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    final hub = FakeHub._(server);
    server.listen(hub._handle);
    return hub;
  }

  Future<void> _handle(HttpRequest request) async {
    if (request.uri.path == '/api/tokens/exchange') {
      exchangeCalls += 1;
      final body = jsonDecode(await utf8.decodeStream(request));
      final code = (body['code'] ?? '').toString();

      if (code != issuedCode || codeUsed) {
        request.response.statusCode = 400;
        request.response
            .write(jsonEncode({'error': 'This pairing code is not valid.'}));
      } else {
        codeUsed = true;
        request.response.statusCode = 200;
        request.response.write(jsonEncode({
          'token': 'tmu_abc123:s3cret',
          'token_id': 'abc123',
          'email': 'tech@x.com',
          'device_name': 'This PC',
          'capabilities': ['view', 'issue_commands'],
          'expires_at': 1780000000,
        }));
      }
      await request.response.close();
      return;
    }
    request.response.statusCode = 404;
    await request.response.close();
  }

  Future<void> stop() => _server.close(force: true);
}

/// Stands in for the system browser: reads the redirect the app asked for and calls back
/// into the loopback listener, optionally lying about the code or the state.
Future<bool> Function(Uri) fakeBrowser({
  String? code,
  String? state,
  bool silent = false,
}) {
  return (Uri pairUrl) async {
    if (silent) return true; // opened, but nothing ever comes back
    final redirect = Uri.parse(pairUrl.queryParameters['redirect']!);
    final query = {
      'code': code ?? 'the-one-time-code',
      'state': state ?? pairUrl.queryParameters['state']!,
    };
    // NOT awaited, because `launchUrl` is not: opening a browser returns as soon as the
    // browser has been handed the URL, and everything after that happens on its own. A
    // fake that waited for its own request to be answered would deadlock against the
    // listener the app has not finished setting up -- which is exactly what it did, and
    // is why the listener is now attached before `open` is called.
    unawaited(
      http.get(redirect.replace(queryParameters: query)).catchError((_) {
        // The listener closes as soon as it has answered; a connection error here is the
        // browser's problem, not the flow's.
        return http.Response('', 499);
      }),
    );
    return true;
  };
}

void main() {
  late FakeHub hub;

  setUp(() async => hub = await FakeHub.start());
  tearDown(() async => hub.stop());

  test('the happy path returns a token', () async {
    final pairing = Pairing();
    final result = await pairing.pairViaBrowser(
      hubUrl: hub.url,
      deviceName: 'This PC',
      open: fakeBrowser(),
    );
    expect(result.token, 'tmu_abc123:s3cret');
    expect(result.tokenId, 'abc123');
    expect(result.email, 'tech@x.com');
    expect(result.capabilities, contains('issue_commands'));
    pairing.close();
  });

  test('the pair URL asks for a LOOPBACK redirect and carries a state',
      () async {
    Uri? seen;
    final pairing = Pairing();
    await pairing.pairViaBrowser(
      hubUrl: hub.url,
      deviceName: 'This PC',
      open: (url) async {
        seen = url;
        return fakeBrowser()(url);
      },
    );
    final redirect = Uri.parse(seen!.queryParameters['redirect']!);
    expect(redirect.host, '127.0.0.1');
    expect(redirect.scheme, 'http');
    expect(redirect.port, greaterThan(1023));
    expect(seen!.queryParameters['state'], isNotEmpty);
    pairing.close();
  });

  test('a redirect carrying the WRONG state is refused, not exchanged',
      () async {
    final pairing = Pairing();
    await expectLater(
      pairing.pairViaBrowser(
        hubUrl: hub.url,
        deviceName: 'This PC',
        open: fakeBrowser(state: 'not-the-state-we-generated'),
      ),
      throwsA(isA<PairingException>()),
    );
    // The real assertion: nothing was ever sent to the hub. A state mismatch must fail
    // BEFORE the code is spent, or a redirect the app did not initiate becomes a token.
    expect(hub.exchangeCalls, 0);
    pairing.close();
  });

  test('a redirect carrying no code at all is refused', () async {
    final pairing = Pairing();
    await expectLater(
      pairing.pairViaBrowser(
        hubUrl: hub.url,
        deviceName: 'This PC',
        open: fakeBrowser(code: ''),
      ),
      throwsA(isA<PairingException>()),
    );
    expect(hub.exchangeCalls, 0);
    pairing.close();
  });

  test('the loopback listener does not outlive the pairing', () async {
    Uri? seen;
    final pairing = Pairing();
    await pairing.pairViaBrowser(
      hubUrl: hub.url,
      deviceName: 'This PC',
      open: (url) async {
        seen = url;
        return fakeBrowser()(url);
      },
    );
    pairing.close();

    // A listener left running is a URL that still accepts a code nobody is expecting.
    final redirect = Uri.parse(seen!.queryParameters['redirect']!);
    await expectLater(
      http.get(redirect.replace(queryParameters: {'code': 'x', 'state': 'y'})),
      throwsA(anything),
    );
  });

  test('a code works exactly once', () async {
    final pairing = Pairing();
    await pairing.exchange(hubUrl: hub.url, code: 'the-one-time-code');
    await expectLater(
      pairing.exchange(hubUrl: hub.url, code: 'the-one-time-code'),
      throwsA(isA<PairingException>()),
    );
    pairing.close();
  });

  test("the hub's own refusal message is what the operator sees", () async {
    final pairing = Pairing();
    try {
      await pairing.exchange(hubUrl: hub.url, code: 'wrong');
      fail('should have thrown');
    } on PairingException catch (e) {
      expect(e.message, contains('not valid'));
    }
    pairing.close();
  });

  test('an empty code never reaches the network', () async {
    final pairing = Pairing();
    await expectLater(
      pairing.exchange(hubUrl: hub.url, code: '   '),
      throwsA(isA<PairingException>()),
    );
    expect(hub.exchangeCalls, 0);
    pairing.close();
  });

  test('a bare hostname is assumed to be https', () async {
    final pairing = Pairing();
    // Nothing is listening on this name; the point is only that it was not tried over
    // plaintext http, since a device token must never cross the network in the clear.
    try {
      await pairing.exchange(hubUrl: 'fleethub.invalid', code: 'x');
      fail('should have thrown');
    } on PairingException catch (e) {
      expect(e.message, contains('https://fleethub.invalid'));
    }
    pairing.close();
  });
}
