/// Checking whether a newer client has been released (roadmap #11).
///
/// The client reads the SAME signed manifest the console's Download page renders --
/// `<hub>/download/manifest.json` plus its detached `.sig` -- and **verifies the Ed25519
/// signature against the embedded release public key before believing a word of it.**
/// That is the agent's `SelfUpdater` discipline applied here: an update prompt that
/// trusted an unverified document would be a prompt that anyone who could answer for the
/// hub's hostname could use to point the helpdesk at a binary of their choosing.
///
/// Both endpoints are unauthenticated on the hub, deliberately, so this works before a
/// device has been paired and keeps working after its token is revoked -- an app that
/// could not tell you it was out of date until you signed in would be least useful
/// exactly when it was most stale.
///
/// **The client does NOT install the update itself.** The agent self-updates because it
/// runs unattended as SYSTEM; a desktop app has a person in front of it, and silently
/// swapping the binary under them buys nothing over showing them what changed and letting
/// them click. So this resolves the right build for the platform and hands its URL over --
/// with the sha256 shown, which is the same digest the download page publishes.
///
/// Kept free of Flutter so it can be tested against a fake hub, exactly like pairing.dart.
library;

import 'dart:convert';

import 'package:cryptography/cryptography.dart';
import 'package:http/http.dart' as http;

import '../version.dart';

/// The fleet's release trust root, in its public half -- byte-identical to the agent's
/// `AgentConfig.UpdatePublicKeyHex` and the hub's `clientrelease.RELEASE_PUBLIC_KEY_HEX`.
/// One key for every artifact this project ships, because a second signing key is a second
/// private key to keep safe and a second way for a release to be signed by something
/// nobody meant to trust.
///
/// Safe to embed: it verifies signatures, it cannot make them.
const String releasePublicKeyHex =
    '9a4f433e0eb82fae121fdeede7d2ce881d50bc80021236f24fdfa4494fc0537c';

/// One downloadable build from the manifest.
class ClientBuild {
  const ClientBuild({
    required this.platform,
    required this.arch,
    required this.kind,
    required this.url,
    this.filename = '',
    this.size = 0,
    this.sha256 = '',
    this.label = '',
    this.notes = '',
  });

  final String platform;
  final String arch;
  final String kind; // 'file' or 'link'
  final String url;
  final String filename;
  final int size;
  final String sha256;
  final String label;
  final String notes;

  factory ClientBuild.fromJson(Map<String, dynamic> json) => ClientBuild(
        platform: (json['platform'] ?? '').toString().toLowerCase(),
        arch: (json['arch'] ?? '').toString().toLowerCase(),
        kind: (json['kind'] ?? 'file').toString().toLowerCase(),
        url: (json['url'] ?? '').toString(),
        filename: (json['filename'] ?? '').toString(),
        size: (json['size'] is num) ? (json['size'] as num).toInt() : 0,
        sha256: (json['sha256'] ?? '').toString(),
        label: (json['label'] ?? '').toString(),
        notes: (json['notes'] ?? '').toString(),
      );
}

/// A newer release, and the build to point this machine at.
class AvailableUpdate {
  const AvailableUpdate({
    required this.version,
    required this.build,
    this.notes = '',
  });

  final String version;
  final ClientBuild build;
  final String notes;
}

class UpdateCheckException implements Exception {
  const UpdateCheckException(this.message);
  final String message;
  @override
  String toString() => message;
}

/// Compare two dotted versions numerically.
///
/// Mirrors the hub's `app.cmp_versions` exactly, and the reason it is not a string
/// comparison is the case that motivated the hub's version: `2.10.1` is NEWER than
/// `2.9.9`, and lexically it is not. Missing components are zero, and anything after a
/// `-` suffix is ignored, so `1.2.0-rc1` and `1.2.0` compare equal.
int compareVersions(String a, String b) {
  List<int> parts(String v) => v
      .split('-')
      .first
      .split('.')
      .map((p) => int.tryParse(p.trim()) ?? -1)
      .toList();

  final left = parts(a);
  final right = parts(b);
  final length = left.length > right.length ? left.length : right.length;
  for (var i = 0; i < length; i++) {
    final l = i < left.length ? left[i] : 0;
    final r = i < right.length ? right[i] : 0;
    if (l != r) return l < r ? -1 : 1;
  }
  return 0;
}

class Updater {
  Updater({
    http.Client? client,
    this.currentVersion = clientVersion,
    this.publicKeyHex = releasePublicKeyHex,
  }) : _client = client ?? http.Client();

  final http.Client _client;
  final String currentVersion;

  /// The key this client will accept a release from. A field rather than a hardcoded
  /// reference so the tests can sign with an ephemeral key and still exercise the REAL
  /// verifier -- a verifier tested against a fake verifier tests nothing. It defaults to
  /// the embedded release key, so nothing in the app has to remember to pass it.
  final String publicKeyHex;

  /// Bounded tightly on purpose. This runs at startup and must never be able to delay the
  /// app reaching its fleet list -- an update check is the least urgent thing it does.
  static const Duration timeout = Duration(seconds: 10);

  /// Verify a manifest's detached signature over its EXACT bytes.
  ///
  /// Byte-exact, for the reason `.gitattributes` pins the manifest to `-text`: a signature
  /// covers bytes, and a line-ending rewrite between signing and serving is
  /// indistinguishable from tampering. Which is the correct outcome -- but only if nothing
  /// here re-encodes the input first, which is why this takes bytes and not a decoded map.
  static Future<bool> verify(
    List<int> manifestBytes,
    String signatureHex, {
    String publicKeyHex = releasePublicKeyHex,
  }) async {
    List<int> unhex(String value) {
      final clean = value.trim();
      if (clean.length.isOdd) throw const FormatException('odd-length hex');
      return [
        for (var i = 0; i < clean.length; i += 2)
          int.parse(clean.substring(i, i + 2), radix: 16),
      ];
    }

    try {
      final algorithm = Ed25519();
      final key = SimplePublicKey(
        unhex(publicKeyHex),
        type: KeyPairType.ed25519,
      );
      return await algorithm.verify(
        manifestBytes,
        signature: Signature(unhex(signatureHex), publicKey: key),
      );
    } catch (_) {
      // Malformed hex, a wrong-length key, a truncated signature: none of those is
      // "valid", and none of them should reach the caller as a crash. Fails closed.
      return false;
    }
  }

  /// Fetch, verify, and decide. Returns null when this client is already current.
  ///
  /// Throws [UpdateCheckException] only for things worth telling somebody about. A hub
  /// with no client release published yet answers 404, which is not a problem and is
  /// reported as "nothing newer" rather than as an error the user has to dismiss.
  Future<AvailableUpdate?> check({
    required String hubUrl,
    String platform = 'windows',
    String arch = 'x64',
  }) async {
    final base = hubUrl.trim().replaceAll(RegExp(r'/+$'), '');
    if (base.isEmpty) return null;

    http.Response manifestResponse;
    http.Response signatureResponse;
    try {
      manifestResponse = await _client
          .get(Uri.parse('$base/download/manifest.json'))
          .timeout(timeout);
      // Nothing published yet. Not a problem, and not an error somebody has to dismiss.
      if (manifestResponse.statusCode == 404) return null;
      signatureResponse = await _client
          .get(Uri.parse('$base/download/manifest.json.sig'))
          .timeout(timeout);
    } on Exception catch (e) {
      throw UpdateCheckException('Could not reach $base: $e');
    }

    if (manifestResponse.statusCode != 200 ||
        signatureResponse.statusCode != 200) {
      throw const UpdateCheckException(
        'The hub did not serve a client release manifest.',
      );
    }

    // bodyBytes, not body: `body` decodes through a charset and would hand the verifier
    // re-encoded bytes rather than the ones that were signed.
    final bytes = manifestResponse.bodyBytes;
    if (!await verify(bytes, signatureResponse.body,
        publicKeyHex: publicKeyHex)) {
      throw const UpdateCheckException(
        'This hub is offering a client release that is not signed by the FleetHub '
        'release key. Nothing will be downloaded.',
      );
    }

    final Map<String, dynamic> doc;
    try {
      doc = jsonDecode(utf8.decode(bytes)) as Map<String, dynamic>;
    } catch (_) {
      throw const UpdateCheckException(
        'The client release manifest could not be read.',
      );
    }

    final version = (doc['version'] ?? '').toString().trim();
    if (version.isEmpty) return null;
    if (compareVersions(version, currentVersion) <= 0) return null;

    final builds = ((doc['builds'] as List?) ?? const [])
        .whereType<Map<String, dynamic>>()
        .map(ClientBuild.fromJson)
        .toList(growable: false);

    final build = pickBuild(builds, platform: platform, arch: arch);
    if (build == null) {
      // A release that exists but has no build for this machine is NOT an update, and
      // saying so would send somebody to a download page with nothing on it for them.
      return null;
    }

    return AvailableUpdate(
      version: version,
      build: build,
      notes: (doc['notes'] ?? '').toString(),
    );
  }

  /// The build for this machine, or null.
  ///
  /// An exact platform+arch match wins; failing that, any build for the platform, because
  /// a manifest that names no arch is describing a single universal build rather than
  /// nothing at all.
  static ClientBuild? pickBuild(
    List<ClientBuild> builds, {
    required String platform,
    required String arch,
  }) {
    for (final build in builds) {
      if (build.platform == platform && build.arch == arch) return build;
    }
    for (final build in builds) {
      if (build.platform == platform && build.arch.isEmpty) return build;
    }
    for (final build in builds) {
      if (build.platform == platform) return build;
    }
    return null;
  }

  void close() => _client.close();
}
