/// Small pieces every screen needs (roadmap #11).
library;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:intl/intl.dart';

import '../strings.dart';

/// Renders an [AsyncValue] without ever blanking a screen that already had content.
///
/// This is the whole reason it exists rather than `value.when(...)` at each call site: a
/// poll that fails on a flaky link must leave the last good fleet list on screen with the
/// error beside it. An operator watching a fleet from a train needs the roster from
/// thirty seconds ago far more than they need a spinner.
class AsyncBody<T> extends StatelessWidget {
  const AsyncBody({super.key, required this.value, required this.builder});

  final AsyncValue<T> value;
  final Widget Function(T data) builder;

  @override
  Widget build(BuildContext context) {
    final data = value.valueOrNull;
    final error = value.error;

    if (data == null) {
      if (value.isLoading) {
        return const Center(child: CircularProgressIndicator());
      }
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Text('$error',
              textAlign: TextAlign.center,
              style: TextStyle(color: Theme.of(context).colorScheme.error)),
        ),
      );
    }

    return Column(
      children: [
        if (error != null)
          MaterialBanner(
            content: Text('$error'),
            leading: const Icon(Icons.cloud_off),
            backgroundColor: Theme.of(context).colorScheme.errorContainer,
            actions: const [SizedBox.shrink()],
          ),
        Expanded(child: builder(data)),
      ],
    );
  }
}

class EmptyState extends StatelessWidget {
  const EmptyState({super.key, required this.title, this.hint});

  final String title;
  final String? hint;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(32),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(title, style: Theme.of(context).textTheme.titleMedium),
            if (hint != null) ...[
              const SizedBox(height: 8),
              Text(hint!,
                  textAlign: TextAlign.center,
                  style: Theme.of(context).textTheme.bodySmall),
            ],
          ],
        ),
      ),
    );
  }
}

class PageHeader extends StatelessWidget {
  const PageHeader({super.key, required this.title, this.trailing});

  final String title;
  final Widget? trailing;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(24, 24, 24, 12),
      child: Row(
        children: [
          Expanded(
              child: Text(title,
                  style: Theme.of(context).textTheme.headlineSmall)),
          if (trailing != null) trailing!,
        ],
      ),
    );
  }
}

String formatEpoch(int? epoch) {
  if (epoch == null || epoch == 0) return strings.never;
  return DateFormat.yMMMd()
      .add_Hm()
      .format(DateTime.fromMillisecondsSinceEpoch(epoch * 1000));
}

String formatUptime(int? seconds) {
  if (seconds == null || seconds <= 0) return '--';
  final days = seconds ~/ 86400;
  final hours = (seconds % 86400) ~/ 3600;
  final minutes = (seconds % 3600) ~/ 60;
  if (days > 0) return '${days}d ${hours}h';
  if (hours > 0) return '${hours}h ${minutes}m';
  return '${minutes}m';
}

void showSnack(BuildContext context, String message) {
  ScaffoldMessenger.of(context)
    ..clearSnackBars()
    ..showSnackBar(SnackBar(content: Text(message)));
}
