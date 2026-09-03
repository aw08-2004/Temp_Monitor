// Model parsing (roadmap #11).
//
// The property being pinned is TOLERANCE, not correctness on a happy path: an app one
// release behind the hub must degrade to a blank field, never to an exception that
// empties the fleet list. So most of these feed the decoders things a hub could
// plausibly send -- an added column, a missing one, a number as a string -- and assert
// that nothing throws.

import 'package:flutter_test/flutter_test.dart';
import 'package:fleethub_client/models.dart';

void main() {
  group('Machine', () {
    test('reads a full row', () {
      final m = Machine.fromJson(const {
        'machine': 'PC-1',
        'online': true,
        'temp': 71.5,
        'threshold': 85,
        'last_seen': 1750000000,
        'model': 'OptiPlex 7090',
        'serial_number': 'ABC123',
        'asset_tag': 'IT-0042',
        'service_tag': '7X2Q9',
        'uptime_seconds': 90000,
      });
      expect(m.name, 'PC-1');
      expect(m.online, isTrue);
      expect(m.temp, 71.5);
      expect(m.looksHot, isFalse);
      expect(m.serviceTag, '7X2Q9');
    });

    test('an unknown field is ignored rather than fatal', () {
      final m = Machine.fromJson(const {
        'machine': 'PC-2',
        'online': false,
        'a_column_added_next_release': {'nested': true},
      });
      expect(m.name, 'PC-2');
      expect(m.online, isFalse);
    });

    test('a missing field is null, not an exception', () {
      final m = Machine.fromJson(const {'machine': 'PC-3'});
      expect(m.temp, isNull);
      expect(m.lastSeen, isNull);
      // An agent that predates service-tag reporting simply leaves it blank -- the same
      // fallback the hub documents.
      expect(m.serviceTag, '');
    });

    test('numbers arriving as strings still parse', () {
      final m = Machine.fromJson(const {'machine': 'PC-4', 'temp': '66.25'});
      expect(m.temp, 66.25);
    });

    test('looksHot needs BOTH a reading and a threshold', () {
      expect(Machine.fromJson(const {'machine': 'x', 'temp': 99}).looksHot,
          isFalse);
      expect(
          Machine.fromJson(const {'machine': 'x', 'temp': 99, 'threshold': 85})
              .looksHot,
          isTrue);
      expect(
          Machine.fromJson(const {'machine': 'x', 'temp': 84, 'threshold': 85})
              .looksHot,
          isFalse);
    });
  });

  group('WakeRequest', () {
    test('open states are not settled -- a packet is not an outcome', () {
      for (final status in ['pending', 'relaying', 'sent']) {
        expect(WakeRequest.fromJson({'id': '1', 'status': status}).settled,
            isFalse,
            reason: '$status is still in flight');
      }
    });

    test('every terminal state is settled, including the non-successes', () {
      for (final status in [
        'awake',
        'already_awake',
        'no_relay',
        'no_answer',
        'unwakeable',
        'cancelled'
      ]) {
        expect(
            WakeRequest.fromJson({'id': '1', 'status': status}).settled, isTrue,
            reason: '$status is an ending');
      }
    });

    test('a status this app has never heard of is settled, not open', () {
      // Fails in the safe direction: an unknown state shown once beats a row that spins
      // forever waiting for an outcome that already happened.
      expect(
          WakeRequest.fromJson(const {'id': '1', 'status': 'teleported'})
              .settled,
          isTrue);
    });
  });

  group('DeviceCapabilities', () {
    test('reads the effective set', () {
      final c = DeviceCapabilities.fromJson(const {
        'email': 'tech@x.com',
        'capabilities': ['view', 'issue_commands'],
      });
      expect(c.canView, isTrue);
      expect(c.canIssueCommands, isTrue);
    });

    test('no capabilities means no buttons, not a crash', () {
      final c = DeviceCapabilities.fromJson(const {'email': 'x@y.com'});
      expect(c.canView, isFalse);
      expect(c.canIssueCommands, isFalse);
    });
  });

  group('Alert', () {
    // `detail` is an OBJECT and has been since the rules engine landed -- the hub json-
    // encodes {rule_id, rule_name, text, count} into it. This test used to assert that a
    // STRING detail was accepted as the body, which no hub has ever sent; it passed for as
    // long as it did only because nothing else read the field. The silent failure now
    // pinned is the one that matters: a raw Dart map rendered into the alerts list.
    test('reads the rule sentence out of the detail object', () {
      expect(
          Alert.fromJson(const {
            'id': '1',
            'detail': {'rule_name': 'Disk low', 'text': 'C: is at 95%'},
          }).message,
          'C: is at 95%');
      expect(
          Alert.fromJson(const {
            'id': '1',
            'detail': {'rule_name': 'Disk low'},
          }).message,
          'Disk low');
    });

    test('message wins over the detail, and neither is fatal', () {
      expect(
          Alert.fromJson(const {'id': '1', 'message': 'hot'}).message, 'hot');
      // A detail shape this app has never heard of, and a null one. Blank, not a map
      // printed at an operator, and not an exception that empties the alerts list.
      expect(
          Alert.fromJson(const {
            'id': '1',
            'detail': {'count': 3},
          }).message,
          '');
      expect(Alert.fromJson(const {'id': '1', 'detail': null}).message, '');
    });
  });
}
