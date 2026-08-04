/// Pairing this device with a hub (roadmap #11), following RFC 8252's native-app flow.
///
/// The hub signs people in with OAuth/OIDC and nothing else -- there is no password to
/// type into an app, and there must not be one. So the app proves who it belongs to by
/// driving a real sign-in in the SYSTEM browser and catching a one-time code on a local
/// loopback listener:
///
///   1. bind an HTTP server to 127.0.0.1 on an ephemeral port
///   2. open the browser at `<hub>/app/pair?redirect=http://127.0.0.1:<port>/cb&state=...`
///   3. the hub authenticates that browser, shows a consent page, mints a GRANT
///   4. the browser is redirected to our loopback URL carrying the code
///   5. we exchange the code for the token, once
///
/// Three details are load-bearing rather than incidental:
///
///   * **The system browser, not an embedded webview.** The user can see the address bar
///     and the identity provider's own session; the app never sees the credentials, and
///     an embedded webview would make both of those false.
///   * **`state` is generated here and checked on the way back.** It is what makes a
///     redirect that did not originate from this pairing attempt a refusal rather than a
///     token. The listener also only ever accepts ONE request and then closes.
///   * **The listener is bound to the loopback interface**, not to 0.0.0.0. Binding
///     anywhere else would put a URL that hands over a fleet credential on the local
///     network for the length of the pairing.
///
/// The manual path exists for the same flow reached differently: some environments will
/// not let an app listen locally, and a phone will use a custom URL scheme in phase 2.
/// Both end at the same single-use exchange, so it is never a second mechanism with its
/// own rules.
library;

import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math';

import 'package:http/http.dart' as http;
import 'package:url_launcher/url_launcher.dart';

/// What the exchange gives back. The token is returned once and is never fetchable again
/// -- the hub stores only its hash.
class PairingResult {
  const PairingResult({
    required this.token,
    required this.tokenId,
    required this.email,
    required this.deviceName,
    required this.capabilities,
    required this.expiresAt,
  });

  final String token;
  final String tokenId;
  final String email;
  final String deviceName;
  final List<String> capabilities;
  final int expiresAt;

  factory PairingResult.fromJson(Map<String, dynamic> json) => PairingResult(
        token: (json['token'] ?? '').toString(),
        tokenId: (json['token_id'] ?? '').toString(),
        email: (json['email'] ?? '').toString(),
        deviceName: (json['device_name'] ?? '').toString(),
        capabilities: ((json['capabilities'] as List?) ?? const [])
            .map((c) => c.toString())
            .toList(growable: false),
        expiresAt: (json['expires_at'] is num)
            ? (json['expires_at'] as num).toInt()
            : 0,
      );
}

class PairingException implements Exception {
  const PairingException(this.message);
  final String message;
  @override
  String toString() => message;
}

String _randomState() {
  final rng = Random.secure();
  final bytes = List<int>.generate(24, (_) => rng.nextInt(256));
  return base64Url.encode(bytes).replaceAll('=', '');
}

String _normalizeHubUrl(String raw) {
  var url = raw.trim();
  if (url.isEmpty) {
    throw const PairingException('Enter the address of your hub.');
  }
  if (!url.startsWith('http://') && !url.startsWith('https://')) {
    url = 'https://$url';
  }
  return url.replaceAll(RegExp(r'/+$'), '');
}

class Pairing {
  Pairing({http.Client? client}) : _client = client ?? http.Client();

  final http.Client _client;

  /// How long to wait for the browser round-trip before giving up and closing the
  /// listener. Generous, because it may include signing in to an identity provider with
  /// MFA -- but bounded, because a listener left open forever is a loose end.
  static const Duration browserTimeout = Duration(minutes: 5);

  /// Run the whole loopback flow. Throws [PairingException] with a message meant for a
  /// human on every failure path.
  Future<PairingResult> pairViaBrowser({
    required String hubUrl,
    required String deviceName,
    String platform = 'windows',
    Future<bool> Function(Uri)? open,
  }) async {
    final base = _normalizeHubUrl(hubUrl);
    final state = _randomState();

    // Port 0 = "any free port". Loopback only -- see the library docstring.
    final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    try {
      final redirect = 'http://127.0.0.1:${server.port}/cb';
      final pairUrl = Uri.parse('$base/app/pair').replace(queryParameters: {
        'redirect': redirect,
        'state': state,
        'name': deviceName,
        'platform': platform,
      });

      // The listener is attached BEFORE the browser is opened, and that ordering is the
      // fix for a real race rather than a stylistic choice: a browser that is already
      // running and signed in can complete the whole round trip in milliseconds, and a
      // redirect arriving before `listen` was called is a pairing that hangs until the
      // timeout with no way to tell why.
      final codeFuture = _awaitCode(server, state);

      final launched = await (open ?? _launch)(pairUrl);
      if (!launched) {
        // Nothing will ever answer, so the pending future is abandoned deliberately --
        // without this it completes with a timeout minutes later, as an unhandled error
        // in a flow the user gave up on long before.
        codeFuture.ignore();
        throw const PairingException(
            'Could not open a browser. Pair with a code instead.');
      }

      final code = await codeFuture;
      return exchange(hubUrl: base, code: code);
    } finally {
      // Unconditionally: a listener that outlives its pairing is a URL that still
      // accepts a code nobody is expecting.
      await server.close(force: true);
    }
  }

  Future<bool> _launch(Uri url) =>
      launchUrl(url, mode: LaunchMode.externalApplication);

  Future<String> _awaitCode(HttpServer server, String state) async {
    final completer = Completer<String>();

    final subscription = server.listen((request) async {
      final code = request.uri.queryParameters['code'] ?? '';
      final returned = request.uri.queryParameters['state'] ?? '';

      // Compared before anything else is done with the request. A redirect that did not
      // come from THIS attempt must never be turned into a token.
      final ok = code.isNotEmpty && returned == state;
      request.response
        ..statusCode = ok ? 200 : 400
        ..headers.contentType = ContentType.html
        ..write(ok ? _successPage : _failurePage);
      await request.response.close();

      if (completer.isCompleted) return;
      if (ok) {
        completer.complete(code);
      } else {
        completer.completeError(const PairingException(
            'The browser came back with something this app did not ask for. '
            'Start pairing again.'));
      }
    });

    try {
      return await completer.future.timeout(
        browserTimeout,
        onTimeout: () => throw const PairingException(
            'Pairing timed out. Start again, or pair with a code.'),
      );
    } finally {
      await subscription.cancel();
    }
  }

  /// Exchange a code for a token. Used by both the loopback flow and the manual one --
  /// the code is the credential either way, and it works exactly once.
  Future<PairingResult> exchange({
    required String hubUrl,
    required String code,
  }) async {
    final base = _normalizeHubUrl(hubUrl);
    if (code.trim().isEmpty) {
      throw const PairingException(
          'Enter the pairing code shown in your browser.');
    }

    http.Response response;
    try {
      response = await _client
          .post(Uri.parse('$base/api/tokens/exchange'),
              headers: {'Content-Type': 'application/json'},
              body: jsonEncode({'code': code.trim()}))
          .timeout(const Duration(seconds: 20));
    } on Exception catch (e) {
      throw PairingException('Could not reach $base: $e');
    }

    dynamic body;
    try {
      body = jsonDecode(response.body);
    } catch (_) {
      body = null;
    }
    if (response.statusCode != 200) {
      throw PairingException((body is Map && body['error'] != null)
          ? body['error'].toString()
          : 'The hub refused this pairing code (HTTP ${response.statusCode}).');
    }
    if (body is! Map<String, dynamic> ||
        (body['token'] ?? '').toString().isEmpty) {
      throw const PairingException('The hub returned no token.');
    }
    return PairingResult.fromJson(body);
  }

  void close() => _client.close();
}

const String _successPage = '''
<!doctype html><meta charset="utf-8">
<title>FleetHub</title>
<body style="font-family: system-ui; padding: 3rem; text-align: center">
<h1>Device paired</h1>
<p>You can close this tab and go back to FleetHub.</p>
''';

const String _failurePage = '''
<!doctype html><meta charset="utf-8">
<title>FleetHub</title>
<body style="font-family: system-ui; padding: 3rem; text-align: center">
<h1>Pairing was not completed</h1>
<p>Start pairing again from the app.</p>
''';
