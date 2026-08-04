/// The app's chrome: a navigation rail over the five v1 screens, plus the pairing screen
/// that stands in for all of them until this device has a token (roadmap #11).
library;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../auth/pairing.dart';
import '../auth/token_store.dart';
import '../state.dart';
import '../strings.dart';
import '../update/updater.dart';
import 'alerts_page.dart';
import 'commands_page.dart';
import 'common.dart';
import 'fleet_page.dart';
import 'settings_page.dart';
import 'update_prompt.dart';
import 'wake_page.dart';

class AppShell extends ConsumerStatefulWidget {
  const AppShell({super.key});

  @override
  ConsumerState<AppShell> createState() => _AppShellState();
}

class _AppShellState extends ConsumerState<AppShell> {
  int _index = 0;

  @override
  void initState() {
    super.initState();
    // Once per launch, and only after this device is paired -- the check needs a hub to
    // ask, and the hub it should ask is the one this device belongs to. Deliberately not
    // repeated when the window is re-shown from the tray: a dialog that reappears every
    // time somebody opens the app is one they learn to dismiss unread.
    WidgetsBinding.instance.addPostFrameCallback((_) => _checkForUpdate());
  }

  Future<void> _checkForUpdate() async {
    final session = ref.read(sessionProvider).valueOrNull;
    if (session == null) return;

    final updater = Updater();
    try {
      final update = await updater.check(hubUrl: session.hubUrl);
      if (update == null) return;
      if (!shouldOffer(update, await skippedVersion())) return;
      if (!mounted) return;
      await showUpdateDialog(context, update);
    } on UpdateCheckException catch (e) {
      // Surfaced quietly rather than as a dialog. Two very different things land here --
      // "the hub was unreachable" and "the hub offered a release this app will not
      // trust" -- and the second one deserves to be seen, but neither is worth a modal
      // in front of somebody who opened the app to look at a fleet.
      if (mounted) showSnack(context, e.message);
    } finally {
      updater.close();
    }
  }

  static const _pages = <Widget>[
    FleetPage(),
    AlertsPage(),
    WakePage(),
    CommandsPage(),
    SettingsPage(),
  ];

  @override
  Widget build(BuildContext context) {
    // The alert count rides the rail, so an operator who is looking at the fleet still
    // sees that something is wrong. It is the same badge the console's sidebar carries.
    final openAlerts = ref.watch(alertsProvider).valueOrNull?.length ?? 0;

    return Scaffold(
      body: Row(
        children: [
          NavigationRail(
            selectedIndex: _index,
            onDestinationSelected: (i) => setState(() => _index = i),
            labelType: NavigationRailLabelType.all,
            destinations: [
              const NavigationRailDestination(
                  icon: Icon(Icons.dns_outlined),
                  selectedIcon: Icon(Icons.dns),
                  label: Text('Fleet')),
              NavigationRailDestination(
                icon: Badge(
                  isLabelVisible: openAlerts > 0,
                  label: Text('$openAlerts'),
                  child: const Icon(Icons.notifications_outlined),
                ),
                selectedIcon: const Icon(Icons.notifications),
                label: Text(strings.alerts),
              ),
              NavigationRailDestination(
                  icon: const Icon(Icons.power_settings_new_outlined),
                  selectedIcon: const Icon(Icons.power_settings_new),
                  label: Text(strings.wake)),
              NavigationRailDestination(
                  icon: const Icon(Icons.bolt_outlined),
                  selectedIcon: const Icon(Icons.bolt),
                  label: Text(strings.commands)),
              NavigationRailDestination(
                  icon: const Icon(Icons.settings_outlined),
                  selectedIcon: const Icon(Icons.settings),
                  label: Text(strings.settings)),
            ],
          ),
          const VerticalDivider(width: 1),
          Expanded(child: _pages[_index]),
        ],
      ),
    );
  }
}

/// Connect this device to a hub. Two paths, one grant -- see auth/pairing.dart.
class PairingPage extends ConsumerStatefulWidget {
  const PairingPage({super.key});

  @override
  ConsumerState<PairingPage> createState() => _PairingPageState();
}

class _PairingPageState extends ConsumerState<PairingPage> {
  final _hub = TextEditingController();
  final _device = TextEditingController(text: 'This PC');
  final _code = TextEditingController();

  bool _busy = false;
  bool _manual = false;
  String? _error;

  @override
  void dispose() {
    _hub.dispose();
    _device.dispose();
    _code.dispose();
    super.dispose();
  }

  Future<void> _run(Future<PairingResult> Function(Pairing) action) async {
    setState(() {
      _busy = true;
      _error = null;
    });
    final pairing = Pairing();
    try {
      final result = await action(pairing);
      await ref.read(sessionProvider.notifier).adopt(StoredSession(
            hubUrl: _hub.text.trim(),
            token: result.token,
            tokenId: result.tokenId,
            email: result.email,
          ));
    } on PairingException catch (e) {
      if (mounted) setState(() => _error = e.message);
    } catch (e) {
      if (mounted) setState(() => _error = '$e');
    } finally {
      pairing.close();
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: Center(
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 480),
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(32),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Text(strings.pairTitle,
                    style: Theme.of(context).textTheme.headlineSmall),
                const SizedBox(height: 8),
                Text(strings.pairIntro,
                    style: Theme.of(context).textTheme.bodyMedium),
                const SizedBox(height: 24),
                TextField(
                  controller: _hub,
                  enabled: !_busy,
                  decoration: InputDecoration(
                      labelText: strings.hubAddress,
                      hintText: strings.hubAddressHint,
                      border: const OutlineInputBorder()),
                ),
                const SizedBox(height: 16),
                TextField(
                  controller: _device,
                  enabled: !_busy,
                  decoration: InputDecoration(
                      labelText: strings.deviceName,
                      hintText: strings.deviceNameHint,
                      border: const OutlineInputBorder()),
                ),
                if (_manual) ...[
                  const SizedBox(height: 16),
                  TextField(
                    controller: _code,
                    enabled: !_busy,
                    decoration: InputDecoration(
                        labelText: strings.pairingCode,
                        border: const OutlineInputBorder()),
                  ),
                ],
                const SizedBox(height: 24),
                FilledButton(
                  onPressed: _busy
                      ? null
                      : () => _run((p) => _manual
                          ? p.exchange(hubUrl: _hub.text, code: _code.text)
                          : p.pairViaBrowser(
                              hubUrl: _hub.text, deviceName: _device.text)),
                  child: Text(_busy
                      ? strings.pairWaiting
                      : (_manual
                          ? strings.pairCodeButton
                          : strings.pairButton)),
                ),
                TextButton(
                  onPressed:
                      _busy ? null : () => setState(() => _manual = !_manual),
                  child:
                      Text(_manual ? strings.pairButton : strings.pairManual),
                ),
                if (_error != null) ...[
                  const SizedBox(height: 16),
                  Text(_error!,
                      style: TextStyle(
                          color: Theme.of(context).colorScheme.error)),
                ],
              ],
            ),
          ),
        ),
      ),
    );
  }
}
