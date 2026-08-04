// The update check, against a fake hub that really signs its manifest (roadmap #11).
//
// The point of these is the REFUSALS. This code decides whether to point an operator at
// an executable, so "a manifest that does not verify is not an update" has to be true for
// every way it can fail to verify -- an unsigned document, a document signed by the wrong
// key, and a document edited after signing.
//
// The signing here is real Ed25519 with an ephemeral keypair, not a stub, because a
// verifier tested against a fake verifier tests nothing.

import 'dart:convert';
import 'dart:io';

import 'package:cryptography/cryptography.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:fleethub_client/update/updater.dart';
import 'package:fleethub_client/ui/update_prompt.dart';

String hex(List<int> bytes) =>
    bytes.map((b) => b.toRadixString(16).padLeft(2, '0')).join();

/// A hub that serves a manifest and a detached signature, exactly as the real one does.
class FakeHub {
  FakeHub._(this._server);

  final HttpServer _server;

  List<int>? manifestBytes;
  String signatureHex = '';

  String get url => 'http://127.0.0.1:${_server.port}';

  static Future<FakeHub> start() async {
    final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    final hub = FakeHub._(server);
    server.listen(hub._handle);
    return hub;
  }

  Future<void> _handle(HttpRequest request) async {
    final bytes = manifestBytes;
    if (bytes == null) {
      // A hub with no client release published -- the state every hub is in until the
      // first one is cut.
      request.response.statusCode = 404;
      await request.response.close();
      return;
    }
    if (request.uri.path == '/download/manifest.json') {
      request.response.statusCode = 200;
      request.response.add(bytes);
    } else if (request.uri.path == '/download/manifest.json.sig') {
      request.response.statusCode = 200;
      request.response.write(signatureHex);
    } else {
      request.response.statusCode = 404;
    }
    await request.response.close();
  }

  Future<void> stop() => _server.close(force: true);
}

Map<String, dynamic> manifest({
  String version = '2.0.0',
  String platform = 'windows',
  String arch = 'x64',
}) =>
    {
      'version': version,
      'notes': 'Faster fleet list.',
      'builds': [
        {
          'platform': platform,
          'arch': arch,
          'kind': 'file',
          'filename': 'FleetHubClient.zip',
          'size': 1234,
          'sha256': 'ab' * 32,
          'url': 'https://example.test/FleetHubClient.zip',
        },
      ],
    };

void main() {
  late FakeHub hub;
  late SimpleKeyPair keyPair;
  late String publicKeyHex;

  setUp(() async {
    hub = await FakeHub.start();
    keyPair = await Ed25519().newKeyPair();
    publicKeyHex = hex((await keyPair.extractPublicKey()).bytes);
  });

  tearDown(() async => hub.stop());

  /// Serve `doc`, signed by the ephemeral key unless told otherwise.
  Future<List<int>> publish(Map<String, dynamic> doc,
      {bool sign = true}) async {
    final bytes = utf8.encode(jsonEncode(doc));
    hub.manifestBytes = bytes;
    hub.signatureHex = sign
        ? hex((await Ed25519().sign(bytes, keyPair: keyPair)).bytes)
        : '00' * 64;
    return bytes;
  }

  group('compareVersions', () {
    test('is numeric, not lexical -- 2.10.1 is newer than 2.9.9', () {
      expect(compareVersions('2.10.1', '2.9.9'), greaterThan(0));
    });

    test('zero-pads missing components', () {
      expect(compareVersions('2.8', '2.8.0'), 0);
    });

    test('ignores a pre-release suffix', () {
      expect(compareVersions('3.0.1-rc1', '3.0.1'), 0);
    });

    test('garbage sorts lowest rather than throwing', () {
      expect(compareVersions('garbage', '0.0.1'), lessThan(0));
    });
  });

  group('verify', () {
    test('accepts a real signature over the exact bytes', () async {
      final bytes = await publish(manifest());
      expect(
        await Updater.verify(bytes, hub.signatureHex,
            publicKeyHex: publicKeyHex),
        isTrue,
      );
    });

    test('refuses one byte changed after signing', () async {
      final bytes = await publish(manifest(version: '2.0.0'));
      final tampered = utf8.encode(
        utf8.decode(bytes).replaceFirst('2.0.0', '9.9.9'),
      );
      expect(
        await Updater.verify(tampered, hub.signatureHex,
            publicKeyHex: publicKeyHex),
        isFalse,
      );
    });

    test('refuses a signature made by a different key', () async {
      final bytes = await publish(manifest());
      final other = await Ed25519().newKeyPair();
      final otherHex = hex((await other.extractPublicKey()).bytes);
      expect(
        await Updater.verify(bytes, hub.signatureHex, publicKeyHex: otherHex),
        isFalse,
      );
    });

    test('fails closed on malformed hex rather than throwing', () async {
      final bytes = await publish(manifest());
      expect(await Updater.verify(bytes, 'not-hex'), isFalse);
      expect(await Updater.verify(bytes, 'abc'), isFalse); // odd length
      expect(await Updater.verify(bytes, ''), isFalse);
    });
  });

  group('check', () {
    test('offers a newer signed release', () async {
      await publish(manifest(version: '2.0.0'));
      final updater =
          Updater(currentVersion: '1.0.0', publicKeyHex: publicKeyHex);
      final update = await updater.check(hubUrl: hub.url);
      expect(update, isNotNull);
      expect(update!.version, '2.0.0');
      expect(update.build.url, 'https://example.test/FleetHubClient.zip');
      expect(update.notes, 'Faster fleet list.');
      updater.close();
    });

    test('says nothing when the release is the running version', () async {
      await publish(manifest(version: '1.0.0'));
      final updater =
          Updater(currentVersion: '1.0.0', publicKeyHex: publicKeyHex);
      expect(await updater.check(hubUrl: hub.url), isNull);
      updater.close();
    });

    test('says nothing when the release is OLDER than what is installed',
        () async {
      // A hub that has been rolled back must not push a running client backwards.
      await publish(manifest(version: '0.9.0'));
      final updater =
          Updater(currentVersion: '1.0.0', publicKeyHex: publicKeyHex);
      expect(await updater.check(hubUrl: hub.url), isNull);
      updater.close();
    });

    test('a hub with no release published is not an error', () async {
      hub.manifestBytes = null;
      final updater =
          Updater(currentVersion: '1.0.0', publicKeyHex: publicKeyHex);
      expect(await updater.check(hubUrl: hub.url), isNull);
      updater.close();
    });

    test('an UNSIGNED manifest is refused, loudly, and is never an update',
        () async {
      await publish(manifest(version: '2.0.0'), sign: false);
      final updater =
          Updater(currentVersion: '1.0.0', publicKeyHex: publicKeyHex);
      await expectLater(
        updater.check(hubUrl: hub.url),
        throwsA(isA<UpdateCheckException>()),
      );
      updater.close();
    });

    test('a release with no build for this machine is not an update', () async {
      // Otherwise an Android-only release would send a Windows operator to a download
      // page with nothing on it for them.
      await publish(manifest(version: '2.0.0', platform: 'android', arch: ''));
      final updater =
          Updater(currentVersion: '1.0.0', publicKeyHex: publicKeyHex);
      expect(
        await updater.check(hubUrl: hub.url, platform: 'windows'),
        isNull,
      );
      updater.close();
    });

    test('an unreachable hub is reported, not swallowed', () async {
      final updater =
          Updater(currentVersion: '1.0.0', publicKeyHex: publicKeyHex);
      await expectLater(
        updater.check(hubUrl: 'http://127.0.0.1:1'),
        throwsA(isA<UpdateCheckException>()),
      );
      updater.close();
    });
  });

  group('pickBuild', () {
    final builds = [
      const ClientBuild(
          platform: 'windows', arch: 'arm64', kind: 'file', url: 'a'),
      const ClientBuild(
          platform: 'windows', arch: 'x64', kind: 'file', url: 'b'),
      const ClientBuild(platform: 'android', arch: '', kind: 'file', url: 'c'),
    ];

    test('prefers an exact platform+arch match', () {
      expect(
        Updater.pickBuild(builds, platform: 'windows', arch: 'x64')?.url,
        'b',
      );
    });

    test('falls back to a universal build for the platform', () {
      expect(
        Updater.pickBuild(builds, platform: 'android', arch: 'arm64')?.url,
        'c',
      );
    });

    test('returns null rather than the wrong platform', () {
      expect(Updater.pickBuild(builds, platform: 'ios', arch: 'arm64'), isNull);
    });
  });

  group('shouldOffer', () {
    const update = AvailableUpdate(
      version: '2.0.0',
      build:
          ClientBuild(platform: 'windows', arch: 'x64', kind: 'file', url: 'x'),
    );

    test('offers when nothing has been skipped', () {
      expect(shouldOffer(update, null), isTrue);
      expect(shouldOffer(update, ''), isTrue);
    });

    test('stays quiet about the exact version that was skipped', () {
      expect(shouldOffer(update, '2.0.0'), isFalse);
    });

    test('asks again for the NEXT release', () {
      // The failure this pins is a skip that silences everything from then on -- which
      // would eventually silence the release carrying a security fix.
      expect(shouldOffer(update, '1.5.0'), isTrue);
      final later = AvailableUpdate(version: '2.1.0', build: update.build);
      expect(shouldOffer(later, '2.0.0'), isTrue);
    });
  });
}
