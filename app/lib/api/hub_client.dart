/// The one place this app talks to a hub (roadmap #11).
///
/// Deliberately narrow. Three things are true of every request and are enforced here
/// rather than remembered at each call site:
///
///   * **Bearer, never a cookie.** The device token goes in an Authorization header. The
///     app never holds a session cookie, which is what keeps a stolen token from being
///     upgradeable into one (see the hub's permissions_web.set_request_identity).
///   * **`Content-Type: application/json` on every write.** The hub exempts bearer callers
///     from its CSRF content-type rule, so this is not load-bearing for us -- but sending
///     the same shape the console sends means one request format the hub has to support,
///     not two.
///   * **A 401 is a state change, not an error to retry.** It means the token was revoked,
///     expired, or its owner lost their permissions. Retrying cannot help, so it surfaces
///     as [UnauthenticatedException] and the app returns to pairing.
///
/// There is no Socket.IO client here and there should not be one: the hub's is
/// polling-only, pinned by CORS to its own origin, and costs a server thread per open
/// connection. Polling `/api/machines` on a timer is what the console's own Inventory page
/// does, and it needs nothing widened to accommodate us.
library;

import 'dart:convert';

import 'package:http/http.dart' as http;

/// The token is gone, expired, or no longer authorised. Re-pair.
class UnauthenticatedException implements Exception {
  const UnauthenticatedException(
      [this.message = 'This device is no longer signed in.']);
  final String message;
  @override
  String toString() => message;
}

/// The hub answered, and said no.
class HubException implements Exception {
  const HubException(this.statusCode, this.message);
  final int statusCode;
  final String message;
  @override
  String toString() => message;
}

class HubClient {
  HubClient({required this.baseUrl, required this.token, http.Client? inner})
      : _inner = inner ?? http.Client();

  final String baseUrl;
  final String token;
  final http.Client _inner;

  /// Long enough for a hub behind a slow link, short enough that a dead connection does
  /// not leave a screen spinning until somebody force-quits.
  static const Duration timeout = Duration(seconds: 20);

  Map<String, String> get _headers => {
        'Authorization': 'Bearer $token',
        'Accept': 'application/json',
      };

  Uri _url(String path, [Map<String, String>? query]) {
    final base = Uri.parse(baseUrl);
    return base.replace(
      path: '${base.path.replaceAll(RegExp(r'/$'), '')}$path',
      queryParameters: (query == null || query.isEmpty) ? null : query,
    );
  }

  dynamic _decode(http.Response response) {
    if (response.statusCode == 401) {
      throw const UnauthenticatedException();
    }
    dynamic body;
    if (response.body.isNotEmpty) {
      try {
        body = jsonDecode(response.body);
      } catch (_) {
        body = null;
      }
    }
    if (response.statusCode >= 400) {
      final message = (body is Map && body['error'] != null)
          ? body['error'].toString()
          : 'HTTP ${response.statusCode}';
      throw HubException(response.statusCode, message);
    }
    return body;
  }

  Future<dynamic> get(String path, {Map<String, String>? query}) async =>
      _decode(await _inner
          .get(_url(path, query), headers: _headers)
          .timeout(timeout));

  Future<dynamic> post(String path, {Object? body}) async => _decode(
        await _inner
            .post(_url(path),
                headers: {..._headers, 'Content-Type': 'application/json'},
                body: jsonEncode(body ?? const <String, dynamic>{}))
            .timeout(timeout),
      );

  Future<dynamic> delete(String path) async => _decode(
      await _inner.delete(_url(path), headers: _headers).timeout(timeout));

  void close() => _inner.close();
}

/// Every list endpoint returns `{"<key>": [...]}`; this is the one place that shape is
/// unwrapped, so a screen never has to know it.
List<Map<String, dynamic>> rowsOf(dynamic body, String key) {
  if (body is! Map) return const [];
  final rows = body[key];
  if (rows is! List) return const [];
  return rows.whereType<Map<String, dynamic>>().toList(growable: false);
}
