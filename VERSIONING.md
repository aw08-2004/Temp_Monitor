# Versioning

Three version numbers ship from this repo, independently, and each one drives a real
updater that compares numbers and acts on the answer. This file says what the digits mean
and what the format rules are. It is short on purpose — the rules that matter are the ones
some piece of code already depends on.

| Line | Declared in | Compared by |
|---|---|---|
| **Hub** | `HUB_VERSION` in [hub/app.py](hub/app.py) | `hub_update_available()`, against `main` |
| **Agent** | `Version` in [AgentConfig.cs](agent/src/TempMonitorAgent/AgentConfig.cs) **and** `<Version>` in [TempMonitorAgent.csproj](agent/src/TempMonitorAgent/TempMonitorAgent.csproj) | `SelfUpdater`, against the signed manifest |
| **Client** | `clientVersion` in [app/lib/version.dart](app/lib/version.dart) **and** `version:` in [app/pubspec.yaml](app/pubspec.yaml) | `updater.dart`, against the signed client manifest |

## Format

**`MAJOR.MINOR.PATCH`, three numeric components, nothing else.** No `-rc1`, no `+build`,
no letters, never two components and never four. This is not style — three separate
things break:

- `parse_hub_version()` in [hub/app.py](hub/app.py) matches only `["']([\d.]+)["']`, so
  `HUB_VERSION = "1.83.0-rc1"` parses as `None`. The hub does not error; it just stops
  discovering updates, fleet-wide, silently.
- `versionLess()` in [processes.js](hub/static/js/processes.js) and
  [fleet-terminal.js](hub/static/js/fleet-terminal.js) loops over exactly three components
  with `Number(part)`, so a suffix becomes `NaN → 0` and a fourth component is ignored.
- The four comparators (hub `version_tuple`, agent `VersionUtil`, Dart `compareVersions`,
  `release_client.py`) agree on well-formed input and *disagree* on anything else — the
  first two truncate at the first non-digit, the last two score it `-1`. Stay inside the
  format and the disagreement never surfaces.

**Strictly increasing. Never reuse a number, never go backwards.** Every updater in the
fleet asks "is the other side *strictly* newer" — `hub_update_available()`,
`SelfUpdater.cs`, `release_client.py`. So a repeated or lowered number does not roll
anything back; it means *nothing happens at all*, everywhere, and the release looks like
it shipped. This has bitten twice already: an agent released as `1.19.1` (a major-digit
typo, on a train where `1.x` means the long-dead Python companion) and a release commit
that moved the agent from `3.32.0` back to `3.31.0`.

**The two-file pairs must match.** `AgentConfig.Version` ↔ csproj `<Version>`,
`clientVersion` ↔ `pubspec.yaml`. Move them with the scripts, not by hand:

```
agent/release.ps1 -Version 3.32.0
python release_client.py --set-version 1.1.0
```

## What each digit means

### MAJOR — an upgrade that breaks a running deployment

Reserved for exactly that, and nothing else:

- a hub↔agent wire change that agents already in the field cannot survive
- a `.env` or config key that an operator must edit by hand before the new version runs
- a migration that cannot roll back
- removing an API surface an external caller uses (`/api/report`, a token contract)

A major bump also needs a release note and the deploy order spelled out: **hub first**,
then the agent, which propagates in about 15 minutes.

Everything short of that is a minor, however large it looks. The hub has been `1.x`
through a page removal and a full i18n rewrite, and that was right both times.

The agent's major digit carries a second meaning: it names the **train**. `2.x` was the
Python companion (removed), `3.x` is the C# agent. `AGENT_TRAIN_MIN_VERSION = "3.0.0"`
and `get_advertised_version()` in [hub/app.py](hub/app.py) gate on it — and note that a
`4.x` agent would still satisfy `>= 3.0.0`, so moving the agent to `4` is a decision about
that constant and about the install channel, not just a note that something broke.

### MINOR — new capability, or a user-visible change in behaviour

A new page, tab, metric, action or setting; a new field on the wire; a new language; a
reworked flow. Also *removing* a UI surface: dropping the History page took
`1.66.1 → 1.67.0`, correctly — it changed what operators see without breaking any
deployment.

**For the agent this rule has teeth.** Anything the hub might gate on takes a minor,
because the hub hardcodes the minor that introduced a feature:

```js
const MIN_STREAMING_AGENT   = '3.1.0';   // fleet-terminal.js
const MIN_INTERACTIVE_AGENT = '3.2.0';
const MIN_PTY_AGENT         = '3.15.0';
const MIN_PROCESS_AGENT     = '3.24.0';  // processes.js
```

Those constants are a compatibility API. A capability shipped in a patch release leaves
the hub with no clean version to point at.

### PATCH — everything else

Bugfixes, UI polish, a security fix that adds no capability, docs- and tests-only changes,
and (hub only) a bump made purely to trigger a deploy. Example: the sidebar update notice
was rendering while marked `hidden` because a `display: flex` outranked the UA stylesheet
— a one-rule CSS fix, `1.82.1 → 1.82.2`.

## When to bump

**Hub — on every push to `main` that touches `hub/`.** The self-updater compares
`main`'s `HUB_VERSION` to the running one, so a push without a bump never reaches a
deployed hub, whether or not `HUB_AUTO_UPDATE` is on. A push that only touches docs or
`tests/` needs no bump, because there is nothing for a hub to install.

**Agent and client — only when cutting a release.** The number means "what is signed and
downloadable", so it moves in `agent/release.ps1` / `release_client.py`, not in the commit
that writes the feature. Source that sits on `main` unreleased keeps the old number; the
roadmap tracks the gap.

## Not covered by this policy

- `min_version` in a package rule ([hub/packages.py](hub/packages.py)) is the version of
  some *third-party program detected on a machine*. Unrelated to any number above.
- The database has no schema version. Migrations are marker-row based and deliberately
  unnumbered — there is no `PRAGMA user_version` anywhere, and this policy does not
  introduce one.
- `install.ps1` is versionless by design: it always installs whatever is on `main`.

The mechanical half of the above — three components, no suffixes, the two pairs in sync,
the `MIN_*` gates well-formed — is pinned by `test_version_format_policy()` in
[tests/test_versions.py](tests/test_versions.py).
