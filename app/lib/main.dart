/// FleetHub client entry point (roadmap #11).
///
/// Desktop-shaped from the start: the window minimises to a tray icon rather than
/// closing, because an app that is not running cannot raise the alert that is the whole
/// reason it exists. Closing the window hides it; quitting is an explicit choice from the
/// tray menu.
library;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:local_notifier/local_notifier.dart';
import 'package:tray_manager/tray_manager.dart';
import 'package:window_manager/window_manager.dart';

import 'state.dart';
import 'strings.dart';
import 'ui/shell.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();

  await windowManager.ensureInitialized();
  // Registers the Start Menu shortcut carrying an AppUserModelID. Windows will not show a
  // toast for an application without one, and an unsigned non-MSIX build has none of its
  // own -- see notify.dart.
  await localNotifier.setup(appName: 'FleetHub');

  await windowManager.waitUntilReadyToShow(
    const WindowOptions(
      size: Size(1100, 760),
      minimumSize: Size(720, 520),
      title: 'FleetHub',
      titleBarStyle: TitleBarStyle.normal,
    ),
    () async {
      await windowManager.show();
      await windowManager.focus();
    },
  );
  // Hand the close button to us so it can hide instead of exiting.
  await windowManager.setPreventClose(true);

  runApp(const ProviderScope(child: FleetHubApp()));
}

class FleetHubApp extends ConsumerStatefulWidget {
  const FleetHubApp({super.key});

  @override
  ConsumerState<FleetHubApp> createState() => _FleetHubAppState();
}

class _FleetHubAppState extends ConsumerState<FleetHubApp>
    with WindowListener, TrayListener {
  @override
  void initState() {
    super.initState();
    windowManager.addListener(this);
    trayManager.addListener(this);
    _initTray();
  }

  Future<void> _initTray() async {
    // The icon ships as a Windows .ico beside the executable; see app/README.md.
    await trayManager.setIcon('windows/runner/resources/app_icon.ico');
    await trayManager.setToolTip(strings.appTitle);
    await trayManager.setContextMenu(Menu(items: [
      MenuItem(key: 'show', label: 'Open FleetHub'),
      MenuItem.separator(),
      MenuItem(key: 'quit', label: 'Quit'),
    ]));
  }

  @override
  void dispose() {
    windowManager.removeListener(this);
    trayManager.removeListener(this);
    super.dispose();
  }

  @override
  void onWindowClose() async {
    // Hide, do not exit. Polling continues, so an alert raised while the window is shut
    // still reaches the operator as a toast -- which is the point of the tray icon.
    await windowManager.hide();
  }

  @override
  void onTrayIconMouseDown() => windowManager.show();

  @override
  void onTrayMenuItemClick(MenuItem item) async {
    switch (item.key) {
      case 'show':
        await windowManager.show();
        await windowManager.focus();
      case 'quit':
        await windowManager.setPreventClose(false);
        await windowManager.close();
    }
  }

  @override
  Widget build(BuildContext context) {
    // Follows the OS light/dark setting, like the console follows its theme token.
    return MaterialApp(
      title: strings.appTitle,
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
          colorSchemeSeed: const Color(0xFF2E7D6F), useMaterial3: true),
      darkTheme: ThemeData(
          colorSchemeSeed: const Color(0xFF2E7D6F),
          brightness: Brightness.dark,
          useMaterial3: true),
      home: const RootView(),
    );
  }
}

/// Paired or not paired. Nothing else in the app has to ask.
class RootView extends ConsumerWidget {
  const RootView({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final session = ref.watch(sessionProvider);
    return session.when(
      loading: () =>
          const Scaffold(body: Center(child: CircularProgressIndicator())),
      error: (e, _) => Scaffold(body: Center(child: Text('$e'))),
      data: (value) => value == null ? const PairingPage() : const AppShell(),
    );
  }
}
