/// Plain data classes over the hub's existing JSON (roadmap #11).
///
/// **Every one of these is tolerant by construction**: unknown fields are ignored and
/// missing fields become null. This is the same discipline the hub applies to older
/// agents, applied in the other direction -- an app one release behind the hub must
/// degrade to a blank field, never to a parse exception that empties the fleet list. A
/// strict decoder here would turn "the hub added a column" into "the app is broken".
///
/// Nothing in this file talks to the network or knows a hub exists, so it is all testable
/// against captured responses.
library;

double? _asDouble(dynamic value) {
  if (value is num) return value.toDouble();
  if (value is String) return double.tryParse(value);
  return null;
}

int? _asInt(dynamic value) {
  if (value is num) return value.toInt();
  if (value is String) return int.tryParse(value);
  return null;
}

String _asString(dynamic value) => value == null ? '' : value.toString();

bool _asBool(dynamic value) {
  if (value is bool) return value;
  if (value is num) return value != 0;
  if (value is String) return value == 'true' || value == '1';
  return false;
}

/// One machine as `/api/machines` reports it.
class Machine {
  const Machine({
    required this.name,
    required this.online,
    this.temp,
    this.threshold,
    this.lastSeen,
    this.model = '',
    this.serialNumber = '',
    this.assetTag = '',
    this.serviceTag = '',
    this.uptimeSeconds,
  });

  final String name;
  final bool online;
  final double? temp;
  final double? threshold;
  final int? lastSeen;
  final String model;
  final String serialNumber;
  final String assetTag;
  final String serviceTag;
  final int? uptimeSeconds;

  /// True when the hub would raise a temperature alert for this reading. Derived here
  /// rather than sent, because the hub's own alert evaluation is an AVERAGE over a window
  /// and this is one sample -- so this is "looks hot right now", which is a different and
  /// weaker claim than the Alerts list makes, and the UI says so.
  bool get looksHot => temp != null && threshold != null && temp! >= threshold!;

  factory Machine.fromJson(Map<String, dynamic> json) => Machine(
        name: _asString(json['machine'] ?? json['name']),
        online: _asBool(json['online']),
        temp: _asDouble(json['temp'] ?? json['last_temp']),
        threshold: _asDouble(json['threshold']),
        lastSeen: _asInt(json['last_seen']),
        model: _asString(json['model']),
        serialNumber: _asString(json['serial_number']),
        assetTag: _asString(json['asset_tag']),
        serviceTag: _asString(json['service_tag']),
        uptimeSeconds: _asInt(json['uptime_seconds']),
      );
}

/// One open alert as `/api/alerts` reports it.
class Alert {
  const Alert({
    required this.id,
    required this.kind,
    required this.machine,
    this.message = '',
    this.raisedAt,
  });

  final String id;
  final String kind;
  final String machine;
  final String message;
  final int? raisedAt;

  factory Alert.fromJson(Map<String, dynamic> json) => Alert(
        id: _asString(json['id']),
        kind: _asString(json['kind']),
        machine: _asString(json['machine']),
        message: _asString(json['message'] ?? json['detail']),
        raisedAt: _asInt(json['raised_at'] ?? json['created_at']),
      );
}

/// A wake attempt. Every state name the hub uses says "attempt" rather than "success",
/// and this class does not flatten them into a bool for exactly that reason: `sent` means
/// a packet went out, which is not the same as the machine waking, and a UI that showed
/// a tick for it would be lying in the one direction that matters.
class WakeRequest {
  const WakeRequest({
    required this.id,
    required this.machine,
    required this.status,
    this.reason = '',
    this.requestedAt,
  });

  final String id;
  final String machine;
  final String status;
  final String reason;
  final int? requestedAt;

  bool get settled => !const {'pending', 'relaying', 'sent'}.contains(status);

  factory WakeRequest.fromJson(Map<String, dynamic> json) => WakeRequest(
        id: _asString(json['id']),
        machine: _asString(json['machine']),
        status: _asString(json['status']),
        reason: _asString(json['reason']),
        requestedAt: _asInt(json['requested_at']),
      );
}

/// A saved command from `/api/fleet/favorites` -- the app's quick-action list.
class Favorite {
  const Favorite({
    required this.id,
    required this.name,
    required this.commandType,
    this.params = const {},
  });

  final String id;
  final String name;
  final String commandType;
  final Map<String, dynamic> params;

  factory Favorite.fromJson(Map<String, dynamic> json) => Favorite(
        id: _asString(json['id']),
        name: _asString(json['name']),
        commandType: _asString(json['command_type'] ?? json['type']),
        params: json['params'] is Map<String, dynamic>
            ? json['params'] as Map<String, dynamic>
            : const {},
      );
}

/// What this device is allowed to do, from `/api/permissions/me`.
///
/// Presentation only. Every endpoint is gated server-side and the token's ceiling is
/// intersected with the owner's live permissions on every request -- this exists so the
/// app can hide a button rather than offer one that 403s.
class DeviceCapabilities {
  const DeviceCapabilities({required this.email, required this.capabilities});

  final String email;
  final Set<String> capabilities;

  bool get canView => capabilities.contains('view');
  bool get canIssueCommands => capabilities.contains('issue_commands');

  factory DeviceCapabilities.fromJson(Map<String, dynamic> json) =>
      DeviceCapabilities(
        email: _asString(json['email']),
        capabilities: ((json['capabilities'] as List?) ?? const [])
            .map((c) => c.toString())
            .toSet(),
      );
}
