/// Saved commands as quick actions (roadmap #11).
///
/// Deliberately not a command COMPOSER. Favourites are written in the console, where
/// there is room to get a script right and an audit trail that names who wrote it; this
/// screen exists for the other half of that -- running one of them from a car park. A
/// free-text command box on a phone would be the easiest way in this product to run the
/// wrong thing as SYSTEM on the wrong machine.
library;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../models.dart';
import '../state.dart';
import '../strings.dart';
import 'common.dart';

class CommandsPage extends ConsumerStatefulWidget {
  const CommandsPage({super.key});

  @override
  ConsumerState<CommandsPage> createState() => _CommandsPageState();
}

class _CommandsPageState extends ConsumerState<CommandsPage> {
  String? _machine;

  @override
  Widget build(BuildContext context) {
    final favorites = ref.watch(favoritesProvider);
    final machines =
        ref.watch(machinesProvider).valueOrNull ?? const <Machine>[];
    final canIssue =
        ref.watch(capabilitiesProvider).valueOrNull?.canIssueCommands ?? false;

    final online = machines.where((m) => m.online).toList()
      ..sort((a, b) => a.name.toLowerCase().compareTo(b.name.toLowerCase()));

    return Column(
      children: [
        PageHeader(title: strings.commands),
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 24),
          child: DropdownButtonFormField<String>(
            initialValue: _machine,
            decoration: InputDecoration(
                labelText: strings.pickMachine,
                border: const OutlineInputBorder(),
                isDense: true),
            // Only online machines: a command aimed at an offline PC sits queued until
            // its TTL expires, and offering it here would look like it ran.
            items: online
                .map(
                    (m) => DropdownMenuItem(value: m.name, child: Text(m.name)))
                .toList(growable: false),
            onChanged: (value) => setState(() => _machine = value),
          ),
        ),
        const SizedBox(height: 8),
        if (!canIssue)
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 24),
            child: Text(strings.notPermitted,
                style: Theme.of(context).textTheme.bodySmall),
          ),
        Expanded(
          child: favorites.when(
            loading: () => const Center(child: CircularProgressIndicator()),
            error: (e, _) => Center(child: Text('$e')),
            data: (rows) {
              if (rows.isEmpty) {
                return EmptyState(
                    title: strings.noFavorites, hint: strings.noFavoritesHint);
              }
              return ListView.separated(
                itemCount: rows.length,
                separatorBuilder: (_, __) => const Divider(height: 1),
                itemBuilder: (context, i) {
                  final favorite = rows[i];
                  return ListTile(
                    title: Text(favorite.name),
                    subtitle: Text(favorite.commandType),
                    trailing: FilledButton.tonal(
                      onPressed: (canIssue && _machine != null)
                          ? () => _run(favorite)
                          : null,
                      child: const Icon(Icons.play_arrow),
                    ),
                  );
                },
              );
            },
          ),
        ),
      ],
    );
  }

  Future<void> _run(Favorite favorite) async {
    final api = ref.read(apiProvider);
    final machine = _machine;
    if (api == null || machine == null) return;

    // Confirmed, always. This runs code as SYSTEM on somebody's PC, and a single
    // mis-tap on a list of one-line rows is exactly how the wrong machine gets restarted.
    final ok = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text(favorite.name),
        content: Text('Run this on $machine?'),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: Text(strings.cancel)),
          FilledButton(
              onPressed: () => Navigator.pop(context, true),
              child: const Text('Run')),
        ],
      ),
    );
    if (ok != true) return;

    try {
      await api.issueCommand(machine, favorite.commandType,
          params: favorite.params);
      if (mounted) showSnack(context, strings.commandQueued(machine));
    } catch (e) {
      if (mounted) showSnack(context, '$e');
    }
  }
}
