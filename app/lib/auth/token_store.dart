/// Where the device token lives (roadmap #11).
///
/// `flutter_secure_storage`, which on Windows is DPAPI-backed: the stored blob is bound
/// to the Windows user account, so copying it to another machine yields nothing. A token
/// is a months-long credential to a fleet-management console -- a plain file beside the
/// executable would make "somebody copied AppData" and "somebody stole a helpdesk
/// account" the same event.
///
/// The hub URL is stored beside it, not because it is secret but because the two are one
/// fact: a token means nothing without the hub it was minted by, and keeping them in one
/// place removes the state where the app has one and not the other.
library;

import 'package:flutter_secure_storage/flutter_secure_storage.dart';

class StoredSession {
  const StoredSession({
    required this.hubUrl,
    required this.token,
    required this.tokenId,
    required this.email,
  });

  final String hubUrl;
  final String token;
  final String tokenId;
  final String email;
}

class TokenStore {
  TokenStore({FlutterSecureStorage? storage})
      : _storage = storage ??
            const FlutterSecureStorage(
              // Survives a Windows password change rather than being silently
              // undecryptable afterwards, which would look to the user exactly like the
              // hub having revoked them.
              wOptions: WindowsOptions(useBackwardCompatibility: false),
            );

  final FlutterSecureStorage _storage;

  static const _hubUrlKey = 'fleethub.hub_url';
  static const _tokenKey = 'fleethub.token';
  static const _tokenIdKey = 'fleethub.token_id';
  static const _emailKey = 'fleethub.email';

  Future<StoredSession?> read() async {
    final token = await _storage.read(key: _tokenKey);
    final hubUrl = await _storage.read(key: _hubUrlKey);
    // Both or neither. A half-written session would send the app into a loop of failing
    // requests it cannot explain.
    if (token == null || token.isEmpty || hubUrl == null || hubUrl.isEmpty) {
      return null;
    }
    return StoredSession(
      hubUrl: hubUrl,
      token: token,
      tokenId: await _storage.read(key: _tokenIdKey) ?? '',
      email: await _storage.read(key: _emailKey) ?? '',
    );
  }

  Future<void> write(StoredSession session) async {
    await _storage.write(key: _hubUrlKey, value: session.hubUrl);
    await _storage.write(key: _tokenIdKey, value: session.tokenId);
    await _storage.write(key: _emailKey, value: session.email);
    // The token LAST: if writing is interrupted, `read` finds no token and the app pairs
    // again, rather than finding a token it cannot match to a hub.
    await _storage.write(key: _tokenKey, value: session.token);
  }

  Future<void> clear() async {
    // The token FIRST, mirroring `write`: whatever else survives a crash here, the
    // credential does not.
    await _storage.delete(key: _tokenKey);
    await _storage.delete(key: _tokenIdKey);
    await _storage.delete(key: _emailKey);
    await _storage.delete(key: _hubUrlKey);
  }
}
