/// The fleet list and one machine's detail (roadmap #11).
///
/// Sorting mirrors the console's Inventory page deliberately (static/js/inventory.js):
/// online before offline, then by name, with the name as the stable tiebreak. Two views
/// of one fleet that disagree about the order is a small thing that costs an operator
/// real time when they are looking for a row they saw a minute ago in the browser.
library;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../models.dart';
import '../state.dart';
import '../strings.dart';
import 'common.dart';

class FleetPage extends ConsumerStatefulWidget {
  const FleetPage({super.key});

  @override
  ConsumerState<FleetPage> createState() => _FleetPageState();
}

class _FleetPageState extends ConsumerState<FleetPage> {
  String _query = '';

  List<Machine> _visible(List<Machine> machines) {
    final needle = _query.trim().toLowerCase();
    final rows = needle.isEmpty
        ? [...machines]
        // Every identifier the console searches, so "the Dell with service tag 7X2Q9"
        // finds the same row in both places.
        : machines
            .where((m) =>
                m.name.toLowerCase().contains(needle) ||
                m.assetTag.toLowerCase().contains(needle) ||
                m.serialNumber.toLowerCase().contains(needle) ||
                m.serviceTag.toLowerCase().contains(needle))
            .toList();

    rows.sort((a, b) {
      if (a.online != b.online) return a.online ? -1 : 1;
      return a.name.toLowerCase().compareTo(b.name.toLowerCase());
    });
    return rows;
  }

  @override
  Widget build(BuildContext context) {
    final machines = ref.watch(machinesProvider);

    return Column(
      children: [
        PageHeader(
          title: strings.fleet,
          trailing: IconButton(
            tooltip: strings.refresh,
            icon: const Icon(Icons.refresh),
            onPressed: () => ref.read(machinesProvider.notifier).refresh(),
          ),
        ),
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 24),
          child: TextField(
            decoration: InputDecoration(
              prefixIcon: const Icon(Icons.search),
              hintText: strings.searchMachines,
              border: const OutlineInputBorder(),
              isDense: true,
            ),
            onChanged: (value) => setState(() => _query = value),
          ),
        ),
        const SizedBox(height: 12),
        Expanded(
          child: AsyncBody<List<Machine>>(
            value: machines,
            builder: (all) {
              final rows = _visible(all);
              if (rows.isEmpty) {
                return EmptyState(
                    title: strings.noMachines,
                    hint: _query.isEmpty ? strings.noMachinesHint : null);
              }
              return ListView.separated(
                itemCount: rows.length,
                separatorBuilder: (_, __) => const Divider(height: 1),
                itemBuilder: (context, i) => _MachineTile(machine: rows[i]),
              );
            },
          ),
        ),
      ],
    );
  }
}

class _MachineTile extends StatelessWidget {
  const _MachineTile({required this.machine});

  final Machine machine;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return ListTile(
      leading: Icon(
        machine.online ? Icons.circle : Icons.circle_outlined,
        size: 14,
        color: machine.online ? scheme.primary : scheme.outline,
      ),
      title: Text(machine.name),
      subtitle: Text(machine.model.isEmpty
          ? (machine.online ? strings.online : strings.offline)
          : machine.model),
      trailing: machine.temp == null
          ? null
          : Text(
              '${machine.temp!.toStringAsFixed(0)} °C',
              style: TextStyle(
                fontWeight: FontWeight.w600,
                // "Looks hot" from one sample, which is a weaker claim than the Alerts
                // list makes -- the hub raises an alert on an AVERAGE over a window. The
                // colour hints; only the Alerts tab asserts.
                color: machine.looksHot ? scheme.error : null,
              ),
            ),
      onTap: () => Navigator.of(context).push(MaterialPageRoute(
        builder: (_) => MachinePage(name: machine.name),
      )),
    );
  }
}

class MachinePage extends ConsumerWidget {
  const MachinePage({super.key, required this.name});

  final String name;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    // Read from the polled roster rather than fetching again: the list is already being
    // refreshed every ten seconds, so this page updates with it and costs no extra
    // request per machine an operator happens to open.
    final roster = ref.watch(machinesProvider).valueOrNull ?? const <Machine>[];
    Machine? machine;
    for (final candidate in roster) {
      if (candidate.name == name) {
        machine = candidate;
        break;
      }
    }

    return Scaffold(
      appBar: AppBar(title: Text(name)),
      body: machine == null
          ? const Center(child: CircularProgressIndicator())
          : ListView(
              padding: const EdgeInsets.all(24),
              children: [
                _Fact(
                    label: strings.online,
                    value: machine.online ? strings.online : strings.offline),
                _Fact(
                    label: strings.temperature,
                    value: machine.temp == null
                        ? '--'
                        : '${machine.temp!.toStringAsFixed(1)} °C'
                            '${machine.looksHot ? '  ·  ${strings.runningHot}' : ''}'),
                _Fact(
                    label: strings.uptime,
                    value: formatUptime(machine.uptimeSeconds)),
                _Fact(
                    label: strings.lastSeen,
                    value: formatEpoch(machine.lastSeen)),
                const Divider(height: 32),
                _Fact(label: strings.model, value: machine.model),
                _Fact(label: strings.serial, value: machine.serialNumber),
                _Fact(label: strings.assetTag, value: machine.assetTag),
                // Blank on a machine whose agent predates service-tag reporting. That is
                // the older-agent fallback the hub documents, surfaced honestly rather
                // than hidden.
                _Fact(label: strings.serviceTag, value: machine.serviceTag),
              ],
            ),
    );
  }
}

class _Fact extends StatelessWidget {
  const _Fact({required this.label, required this.value});

  final String label;
  final String value;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SizedBox(
            width: 140,
            child: Text(label, style: Theme.of(context).textTheme.bodySmall),
          ),
          Expanded(child: Text(value.isEmpty ? '--' : value)),
        ],
      ),
    );
  }
}
