# Android agent -- working notes

Scratch notes for whoever picks this up next. **Not a design document.** Why the agent is shaped
the way it is belongs in `README.md`; what was decided and what was rejected belongs in
`ROADMAP.MD` under #23 and #22. What is here is the other category: things that cost an hour to
rediscover, are true of the toolchain rather than of the product, and would look like noise in
either of those two files.

Delete an entry when it stops being true. A stale note here is worse than no note, because the
whole value of the file is that it is believed on sight.

## State of play

Everything in roadmap #23 (phases F.1, F.2, A, B, C, D, E, H) and the Android half of #22 is
built and pushed on `feat/android-agent`. **None of it has run on a phone.** The per-phase
validation table lives in `ROADMAP.MD` under *Hardware validation still owed*; that table is the
record, and this file deliberately does not repeat it.

Two things must happen before the QR flow can be exercised at all:

1. Deploy the hub at or above `1.108.0`, because the per-platform manifest routing and the hosted
   APK both live there.
2. Upload the signed APK on the provisioning page. The hub derives the signature checksum itself
   now, so no `apksigner` step and no hand-typed field.

The current blocker on the pilot Galaxy A15 is smaller than it looks: `adb devices` sees
`RF8X70H08DA` but `adb shell` answers `error: closed`. The phone dropped its USB debugging
authorisation. Replug it and tap **Allow USB debugging** on the device.

## Toolchain traps

**An XML comment cannot contain `--`.** MSBuild fails the whole build with `MSB4025` and a line
number that points at the project element rather than the comment. This has bitten seven times
in this repo, always while writing the kind of comment the house style asks for. Both csproj
files are covered by `AndroidResourceXmlTests`, which parses them rather than trusting review.
Write `and` or reword; do not reach for an escape.

**Do not author files containing backslashes through a bash heredoc.** The shell eats them. It
has produced a `SyntaxError` from a collapsed newline escape in a Python test, and a literal null
byte inside a Markdown file where `.\release.ps1` was meant to be. Use the Write or Edit tool for
anything carrying a backslash, which on this platform means most paths.

**The JDK is the usual cause of a broken-looking SDK.** Covered in `README.md` under *Build,
test, run*, and repeated here only because the symptom (`NullReferenceException` inside
`ResolveAndroidTooling`) points nowhere near the cause. Pass `JavaSdkDirectory` explicitly.

**Binding names do not always match the platform documentation.** `WipeDataFlags` spells it
`WipeResetProtectionData`, not `ResetProtectionData`. When a member is missing, grep the
Mono.Android XML documentation in the workload rather than guessing from the Java name.

**`UnsafeCheckOpNoThrow` warns `CA1422` and there is no alternative.** Nothing non-obsolete
covers the API 26 to 36 range for reading an appop. The call in `UsageStatsReader` is suppressed
with a pragma and a written reason; leave both in place.

**Usage events are compared by number, not by constant.** `UsageStatsReader` sums foreground time
from resume and pause events as `1` and `2` because the named constants are either deprecated or
newer than the minimum supported API. The numbers are stable platform values, and the reason is
written at the call site.

## Versions

The version is a **triple**, not a pair: `AgentConfig.Version`, and `<Version>` in both csproj
files, with `ApplicationDisplayVersion` tracking it in the Android one. `release.ps1` keeps all
three in step, and `VersionGateTests` fails the build if they drift.

`ApplicationVersion` is deliberately outside that. It is the integer Android sorts upgrades by,
so it is a build counter and not a release number, and the tests pin that it is not tied to
anything else.

**The agent stays at `0.1.0` on purpose, and the reason has changed.** It used to be the manifest
hazard, where all three agents shared one advertised version. That is gone as of #22. What is
left is the console's `MIN_*_AGENT` gates, which still read a version number, and a low one is
what stops the console offering a phone a terminal it can never open. Raising this number without
first replacing those gates with capability checks makes the console offer features the device
cannot answer.

## Release build

`AndroidLinkMode` is `full` in Release, which takes the APK from about 16 MB to about 9.7 MB.
The reasoning is in the csproj. What matters operationally is the failure mode: full trimming
fails at runtime, not at build time, so a `MissingMethodException` on startup from a type nobody
calls directly means turn this off first and ask questions second.

**The signing certificate must never change.** It is what Android checks before replacing the
installed app, and it is also what the provisioning QR's checksum covers. Changing it invalidates
every printed code and makes every installed agent refuse every future update, with no recovery
short of a factory reset per device. `release.ps1` prints the certificate digest after publishing
for exactly this comparison. It was confirmed unchanged after full trimming was enabled.

## Testing

```bash
dotnet test agent-android/tests/FleetHubAgent.Core.Tests/FleetHubAgent.Core.Tests.csproj
```

215 tests, no Android SDK needed. The Android project compiles but has no test project of its
own, which is the whole reason `Core` holds every decision worth testing and the platform half
holds only the calls into the framework. When something in `Platform/` or `Policy/` starts making
a decision, move the decision rather than trying to test it there.

`SelfUpdater` takes its trust root through an internal constructor so a test can supply a
manifest it will accept. That seam is internal on purpose. Do not widen it.
