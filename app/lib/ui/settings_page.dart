/// What this device is, and how to stop being it (roadmap #11).
library;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../state.dart';
import '../strings.dart';
import 'common.dart';

class SettingsPage extends ConsumerWidget {
  const SettingsPage({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final session = ref.watch(sessionProvider).valueOrNull;
    final capabilities = ref.watch(capabilitiesProvider).valueOrNull;

    return Column(
      children: [
        PageHeader(title: strings.settings),
        Expanded(
          child: ListView(
            padding: const EdgeInsets.symmetric(horizontal: 24),
            children: [
              ListTile(
                contentPadding: EdgeInsets.zero,
                title: Text(strings.signedInAs),
                subtitle: Text(session?.email ?? '--'),
              ),
              ListTile(
                contentPadding: EdgeInsets.zero,
                title: Text(strings.connectedTo),
                subtitle: Text(session?.hubUrl ?? '--'),
              ),
              ListTile(
                contentPadding: EdgeInsets.zero,
                title: Text(strings.thisDeviceMay),
                // The EFFECTIVE set, from /api/permissions/me -- which is the token's
                // ceiling intersected with the owner's live permissions. Showing what was
                // granted at pairing would be showing a number that is sometimes wrong,
                // and wrong in the reassuring direction.
                subtitle: Text(capabilities == null
                    ? '--'
                    : (capabilities.capabilities.toList()..sort()).join(', ')),
              ),
              const Divider(height: 32),
              Align(
                alignment: Alignment.centerLeft,
                child: OutlinedButton.icon(
                  icon: const Icon(Icons.logout),
                  label: Text(strings.signOut),
                  onPressed: () => _signOut(context, ref),
                ),
              ),
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: Text(strings.signOutHelp,
                    style: Theme.of(context).textTheme.bodySmall),
              ),
            ],
          ),
        ),
      ],
    );
  }

  Future<void> _signOut(BuildContext context, WidgetRef ref) async {
    final api = ref.read(apiProvider);
    final session = ref.read(sessionProvider).valueOrNull;

    // Revoke server-side FIRST, then forget locally. A token that still works but is on
    // no screen is the worst of both -- nobody can see it, and it can still act. If the
    // revoke fails (the hub is unreachable), the local session is still cleared and the
    // device is left in the hub's list for an admin to revoke, which is the honest
    // outcome rather than refusing to sign out.
    if (api != null && session != null && session.tokenId.isNotEmpty) {
      try {
        await api.revokeSelf(session.tokenId);
      } catch (e) {
        if (context.mounted) showSnack(context, '$e');
      }
    }
    await ref.read(sessionProvider.notifier).forget();
  }
}
