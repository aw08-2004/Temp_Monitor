/// Wake-on-LAN: wake an offline machine, and watch what actually happened (roadmap #11).
///
/// **Every state here is an attempt, and the wording never promises more.** Nothing
/// acknowledges a magic packet, so `sent` is reported as "packet sent, waiting", and
/// `no_relay` -- every PC on that subnet being asleep -- is a first-class answer rather
/// than an error. Both of those are the hub's own design (see wake.py); flattening them
/// into a tick here would undo it at the last step, which is where an operator would
/// actually be misled.
library;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../models.dart';
import '../state.dart';
import '../strings.dart';
import 'common.dart';

class WakePage extends ConsumerWidget {
  const WakePage({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final machines =
        ref.watch(machinesProvider).valueOrNull ?? const <Machine>[];
    final offline = machines.where((m) => !m.online).toList()
      ..sort((a, b) => a.name.toLowerCase().compareTo(b.name.toLowerCase()));
    final requests = ref.watch(wakeRequestsProvider);
    final canWake =
        ref.watch(capabilitiesProvider).valueOrNull?.canIssueCommands ?? false;

    return Column(
      children: [
        PageHeader(title: strings.wake),
        if (!canWake)
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 24),
            child: Text(strings.notPermitted,
                style: Theme.of(context).textTheme.bodySmall),
          ),
        Expanded(
          child: ListView(
            padding: const EdgeInsets.symmetric(horizontal: 24),
            children: [
              if (offline.isEmpty)
                EmptyState(title: strings.noMachines)
              else
                ...offline.map((m) => ListTile(
                      contentPadding: EdgeInsets.zero,
                      title: Text(m.name),
                      subtitle: Text(
                          '${strings.lastSeen}: ${formatEpoch(m.lastSeen)}'),
                      trailing: FilledButton.tonal(
                        onPressed:
                            canWake ? () => _wake(context, ref, m.name) : null,
                        child: Text(strings.wakeMachine),
                      ),
                    )),
              const Divider(height: 32),
              Text(strings.wakeHistory,
                  style: Theme.of(context).textTheme.titleMedium),
              const SizedBox(height: 8),
              AsyncValueList(value: requests),
            ],
          ),
        ),
      ],
    );
  }

  Future<void> _wake(
      BuildContext context, WidgetRef ref, String machine) async {
    final api = ref.read(apiProvider);
    if (api == null) return;
    try {
      final request = await api.wake(machine);
      if (context.mounted) {
        // The state it actually reached, not "sent" -- a machine whose whole subnet is
        // asleep gets told so, immediately, instead of being left to wonder.
        showSnack(context, strings.wakeStatus(request.status));
      }
      await ref.read(wakeRequestsProvider.notifier).refresh();
    } catch (e) {
      if (context.mounted) showSnack(context, '$e');
    }
  }
}

/// The wake history, inline. A separate widget only so the list above stays readable.
class AsyncValueList extends StatelessWidget {
  const AsyncValueList({super.key, required this.value});

  final AsyncValue<List<WakeRequest>> value;

  @override
  Widget build(BuildContext context) {
    final rows = value.valueOrNull;
    if (rows == null) {
      return const Padding(
        padding: EdgeInsets.symmetric(vertical: 24),
        child: Center(child: CircularProgressIndicator()),
      );
    }
    if (rows.isEmpty) return EmptyState(title: strings.noWakes);
    return Column(
      children: rows
          .map((r) => ListTile(
                contentPadding: EdgeInsets.zero,
                dense: true,
                title: Text(r.machine),
                subtitle: Text(r.reason.isEmpty
                    ? strings.wakeStatus(r.status)
                    : '${strings.wakeStatus(r.status)} — ${r.reason}'),
                trailing: Text(formatEpoch(r.requestedAt),
                    style: Theme.of(context).textTheme.bodySmall),
              ))
          .toList(growable: false),
    );
  }
}
