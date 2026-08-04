/// The "a newer FleetHub is available" prompt, raised when the app opens (roadmap #11).
///
/// **Asks, never installs.** The agent self-updates because it runs unattended as SYSTEM;
/// this app has a person in front of it, so it shows what changed, publishes the digest,
/// and opens the signed download URL when they say yes. Swapping a running binary under
/// somebody mid-task buys nothing over that.
///
/// **Three ways out, and "Skip this version" is the one that matters.** Without it, an
/// operator who cannot install software today gets the same dialog every single launch,
/// which trains them to dismiss it -- and the dialog they are dismissing without reading
/// is the one that will eventually carry a security fix. The skip is per VERSION, so the
/// next release asks again.
library;

import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:url_launcher/url_launcher.dart';

import '../update/updater.dart';
import '../version.dart';

const String _skippedVersionKey = 'fleethub.update.skipped_version';

Future<String?> skippedVersion() async {
  try {
    return (await SharedPreferences.getInstance())
        .getString(_skippedVersionKey);
  } catch (_) {
    // A preferences store that will not open must not stop an update being offered --
    // failing here in the direction of "ask anyway" is the safe one.
    return null;
  }
}

Future<void> skipVersion(String version) async {
  try {
    await (await SharedPreferences.getInstance()).setString(
      _skippedVersionKey,
      version,
    );
  } catch (_) {
    // Same reasoning inverted: failing to remember a skip means asking again next launch,
    // which is annoying rather than harmful.
  }
}

/// Should this update be offered, given what the operator has already declined?
///
/// Pure, and separated from the dialog precisely so it can be tested: the rule is "skip
/// this exact version", not "skip everything from now on", and getting that backwards
/// would silence every future release including the one that matters.
bool shouldOffer(AvailableUpdate update, String? skipped) {
  if (skipped == null || skipped.isEmpty) return true;
  return compareVersions(update.version, skipped) > 0;
}

Future<void> showUpdateDialog(
  BuildContext context,
  AvailableUpdate update,
) async {
  final theme = Theme.of(context);

  await showDialog<void>(
    context: context,
    builder: (context) => AlertDialog(
      title: const Text('A newer FleetHub is available'),
      content: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
                'You have $clientVersion. ${update.version} has been released.'),
            if (update.notes.isNotEmpty) ...[
              const SizedBox(height: 16),
              Text(update.notes),
            ],
            if (update.build.notes.isNotEmpty) ...[
              const SizedBox(height: 8),
              Text(update.build.notes, style: theme.textTheme.bodySmall),
            ],
            const SizedBox(height: 16),
            // The manifest was signature-verified before this dialog existed. The digest
            // is shown anyway, because it is the same one the console's Download page
            // publishes and somebody checking the file they got should not have to take
            // two different pages' word for two different numbers.
            if (update.build.sha256.isNotEmpty) ...[
              Text('SHA-256', style: theme.textTheme.bodySmall),
              SelectableText(
                update.build.sha256,
                style: theme.textTheme.bodySmall?.copyWith(
                  fontFamily: 'monospace',
                ),
              ),
            ],
          ],
        ),
      ),
      actions: [
        TextButton(
          onPressed: () async {
            await skipVersion(update.version);
            if (context.mounted) Navigator.pop(context);
          },
          child: const Text('Skip this version'),
        ),
        TextButton(
          onPressed: () => Navigator.pop(context),
          child: const Text('Later'),
        ),
        FilledButton(
          onPressed: () async {
            // Handed to the browser rather than downloaded in-process: the URL came out
            // of a signed manifest, and a browser is where a person can see what they are
            // downloading and where their own AV will look at it.
            await launchUrl(
              Uri.parse(update.build.url),
              mode: LaunchMode.externalApplication,
            );
            if (context.mounted) Navigator.pop(context);
          },
          child: const Text('Download'),
        ),
      ],
    ),
  );
}
