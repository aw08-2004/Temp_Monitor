/// Open alerts, and the place a toast leads back to (roadmap #11).
library;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../models.dart';
import '../state.dart';
import '../strings.dart';
import 'common.dart';

class AlertsPage extends ConsumerWidget {
  const AlertsPage({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final alerts = ref.watch(alertsProvider);

    return Column(
      children: [
        PageHeader(
          title: strings.alerts,
          trailing: IconButton(
            tooltip: strings.refresh,
            icon: const Icon(Icons.refresh),
            onPressed: () => ref.read(alertsProvider.notifier).refresh(),
          ),
        ),
        Expanded(
          child: AsyncBody<List<Alert>>(
            value: alerts,
            builder: (rows) {
              if (rows.isEmpty) {
                return EmptyState(
                    title: strings.noAlerts, hint: strings.noAlertsHint);
              }
              return ListView.separated(
                itemCount: rows.length,
                separatorBuilder: (_, __) => const Divider(height: 1),
                itemBuilder: (context, i) => _AlertTile(alert: rows[i]),
              );
            },
          ),
        ),
      ],
    );
  }
}

class _AlertTile extends ConsumerWidget {
  const _AlertTile({required this.alert});

  final Alert alert;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return ListTile(
      leading: Icon(Icons.warning_amber_rounded,
          color: Theme.of(context).colorScheme.error),
      title: Text(alert.machine.isEmpty ? alert.kind : alert.machine),
      subtitle: Text(alert.message.isEmpty ? alert.kind : alert.message),
      trailing: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Text(formatEpoch(alert.raisedAt),
              style: Theme.of(context).textTheme.bodySmall),
          const SizedBox(width: 12),
          TextButton(
            onPressed: () async {
              final api = ref.read(apiProvider);
              if (api == null) return;
              try {
                await api.dismissAlert(alert.id);
                // Re-read rather than removing the row locally: dismissing is a hub-side
                // state change, and the list that matters is the hub's. A row that
                // disappeared here but not there is the kind of divergence nobody
                // notices until it matters.
                await ref.read(alertsProvider.notifier).refresh();
              } catch (e) {
                if (context.mounted) showSnack(context, '$e');
              }
            },
            child: Text(strings.dismiss),
          ),
        ],
      ),
    );
  }
}
