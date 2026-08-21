# FleetHub client (roadmap #11)

The native FleetHub client. One Flutter codebase; **v1 targets Windows desktop**, and
Android/iOS are additions rather than a rewrite.

What it does in v1: the fleet list, one machine's detail, open alerts with Windows toasts,
Wake-on-LAN, and saved commands as quick actions. Console administration — settings, users,
permission groups, firmware — deliberately stays in the browser and cannot be granted to a
device at all.

## How it talks to a hub

Ordinary HTTPS JSON against endpoints the hub already had. **The hub gained authentication
for this feature and no new data endpoints**, which is the design: a screen that needs data
the console does not already serve is a screen outside v1's scope.

Two things this app deliberately does not use:

- **Socket.IO.** The hub's is polling-only, CORS-pinned to its own origin, and costs a
  server thread per open connection. The app polls `/api/machines` every ten seconds —
  which is what the console's own Inventory page does at thirty — and nothing about the
  hub's perimeter has to be widened to accommodate it.
- **The session cookie.** The app holds a bearer **device token**. It is never given a
  session, so a stolen token cannot be upgraded into one.

## Pairing

There is no password to type into this app, because the hub signs people in with
OAuth/OIDC and nothing else. Pairing follows RFC 8252's native-app flow:

1. the app binds a listener to `127.0.0.1` on a free port
2. it opens the **system browser** at `<hub>/app/pair?redirect=…&state=…`
3. the hub authenticates that browser normally and shows a consent page
4. the operator confirms the device and what it may do
5. the browser is redirected to the loopback URL with a one-time code
6. the app exchanges the code for the token, once

A copy-paste code path exists for when the app cannot listen locally — and is the path a
phone will take through a custom URL scheme in phase 2. Both end at the same single-use
exchange.

The token is stored with `flutter_secure_storage`, DPAPI-backed on Windows, so the stored
blob is bound to the Windows user account.

## One instance

The window's X hides to the tray rather than closing, so the ordinary resting state of this
app is **running but invisible**. That makes "the operator double-clicks the exe again" the
normal path rather than an edge case, and it used to buy them a second process.

Two windows would be a cosmetic annoyance. The real cost was **a second alert poller**: each
instance keeps its own delta seen-set, so every new alert toasted twice — which is precisely
the "operator learns to dismiss FleetHub notifications unread" failure the toast rules above
exist to prevent.

A second launch now takes the mutex, finds it held, wakes the running instance and exits with
status 0. The running instance un-hides, un-minimises and takes the foreground — the same end
state as the tray menu's "Open FleetHub".

Three details that are decisions rather than defaults:

- **The guard is in the C++ runner, not in Dart.** In Dart it would run after the engine has
  booted and the window exists, so the losing instance would flash a window before vanishing.
  In `wWinMain` it costs nothing visible.
- **The mutex is session-scoped** (`Local\`, not `Global\`). Two operators on the same box
  over RDP each get a client, each signed in as themselves with their own device token. A
  machine-wide mutex would hand the first one an app and the second one a process that exits
  without explanation.
- **The wake-up is a broadcast** of a registered window message, not a message to a located
  `HWND`. A broadcast reaches hidden top-level windows, which is the state the running
  instance is usually in, and it avoids identifying the other process by window class — the
  runner's is the stock `FLUTTER_RUNNER_WIN32_WINDOW`, which every Flutter app on the machine
  shares. What a broadcast cannot do is carry data, so forwarding a second launch's command
  line (a phase-2 `fleethub://` URL) will need a window lookup; `single_instance.cpp` says so
  at the point where it would go.

## Building

The platform runner directories are **almost entirely** absent from the repo — a Flutter SDK
upgrade should not arrive as a thousand-line diff. Generate them once per checkout:

```bash
cd app && flutter create --platforms=windows .
```

That writes `windows/` over this source without touching `lib/`, `test/` or `pubspec.yaml`.

**Five files under `windows/runner/` are the exception and ARE tracked**, because what they
carry cannot live in `lib/`: `main.cpp` and `single_instance.{h,cpp}` (the guard above, which
has to run before the Flutter engine starts), `Runner.rc` (the `VERSIONINFO` block compiled
into the exe — what Explorer shows on Properties → Details) and `CMakeLists.txt` (which names
the new source). **The command above overwrites all five with boilerplate.** Put them back:

```bash
git checkout app/windows/runner
```

Forgetting to is a silent failure — the build is green, the client just quietly opens a second
copy of itself and reports `com.example` as its publisher — so `release_client.py` checks for
it and refuses the release rather than warning about it.

Then:

```bash
cd app && flutter pub get && flutter test
```

```bash
cd app && flutter build windows --release
```

The build lands in `build/windows/x64/runner/Release/`.

### Toasts need an application identity

Windows will not show a toast for an application that has none, and an unsigned non-MSIX
build has none of its own. `local_notifier` registers a Start Menu shortcut carrying an
AppUserModelID at startup, which supplies one. **If toasts silently do nothing, check that
shortcut before reading the notification code.** MSIX packaging is the cleaner answer and
the upgrade path once there is a code-signing certificate.

### What gets toasted, and what deliberately does not

The app toasts the **delta**, never the list: each poll is diffed against the alert ids it
has already shown. Two rules carry that, and both exist because the failure is an operator
learning to dismiss FleetHub notifications unread:

- **The first poll of a session primes and raises nothing.** Alerts that were already open
  are history, not news.
- **Signing out resets the delta** ([state.dart](lib/state.dart), `SessionController.forget`).
  The notifier is a process-lifetime object, so without this the next operator to pair on
  the same machine would inherit the previous one's seen-set — and be toasted, at sign-in,
  every alert in their scope that the previous operator's scope did not cover.

`notifyNew` returns what it raised so both are testable: a toast does nothing under a test
binding, so a return value is the only way a test can tell "raised nothing" from "raised
nine". `test/session_test.dart` drives the real providers, because the bug that motivated
it was a missing call rather than a wrong unit — and no test that constructs `AlertNotifier`
directly can see one of those.

## Updating

The app checks for a newer release **once per launch**, after this device is paired. It
reads the same signed manifest the console's Download page renders —
`<hub>/download/manifest.json` plus its detached `.sig` — and **verifies the Ed25519
signature against the embedded release key before believing a word of it** ([updater.dart](lib/update/updater.dart)).
An update prompt that trusted an unverified document would be a prompt anyone who could
answer for the hub's hostname could use to point the helpdesk at a binary of their choosing.

It **asks, and never installs**. The agent self-updates because it runs unattended as
SYSTEM; this app has a person in front of it, so the dialog shows what changed, publishes
the SHA-256, and opens the signed download URL if they say yes. "Skip this version" is
per-version, so the next release asks again — without it, an operator who cannot install
software today gets the same dialog every launch and learns to dismiss it unread.

A hub with no client release published answers 404, which is reported as "nothing newer"
rather than as an error to dismiss.

## Releasing

One command:

```bash
python release_client.py
```

**It asks which version to publish**, showing what is in the tree, what is currently
published, and the patch/minor/major bumps:

```
Current version in the tree : 1.0.0
Currently published         : 1.0.0

What should this release be?
  [1] 1.0.1   (patch -- fixes)
  [2] 1.1.0   (minor -- new features)
  [3] 2.0.0   (major -- breaking changes)
  [k] 1.0.0   (keep -- re-release the version already in the tree)
  or type a version like 1.4.2
```

What counts as a patch, a minor or a major here is the same rule the hub and the agent
follow — [VERSIONING.md](../VERSIONING.md).

Blank picks the patch bump rather than "keep", deliberately: keeping is the answer that
silently publishes nothing new — **no installed client is ever offered an update to a
version that is not strictly newer** — so the mistake this prompt exists to prevent should
not also be what you get by pressing Enter. Choosing a version at or below the published
one warns and asks again; re-publishing after a botched upload is legitimate, but it should
be a decision.

Picking a new version rewrites [lib/version.dart](lib/version.dart) and `pubspec.yaml`
before building, because `version.dart` is compiled into the binary and is what the update
check compares against. Commit them with the manifest.

Then it runs `pub get`, `analyze`, `test`, `build windows`, zips the result, and signs the
manifest. **It refuses rather than warns**: a failing analyzer or a red test stops the
release, because a release script that ships a red build is worse than no release script.

Non-interactive forms, for CI or a scripted release:

```bash
python release_client.py --version 1.2.0     # skip the prompt
python release_client.py --keep-version      # publish the tree's version as-is
python release_client.py --set-version 1.1.0 # only bump the files, then stop
```

To also create the GitHub release and upload the asset with `gh`:

```bash
python release_client.py --upload
```

Then commit `hub/client.manifest.json` + `.sig` and deploy the hub. Every `sha256` and
`size` in the manifest is computed from the bytes on disk by `sign_release.py`, never
typed, and signed with the **same Ed25519 key that signs the agent** — one trust root for
every artifact this project ships.

**Windows build prerequisites** (`release_client.py` names each one when the build fails):

- Visual Studio with the **Desktop development with C++** workload. The three components
  `flutter doctor` checks for are **MSVC build tools**, a **Windows SDK**, and **C++ CMake
  tools for Windows**.
- **C++ ATL for the latest v143 build tools (x86 & x64)** — an optional component of that
  workload that `flutter doctor` does NOT check for, because a bare Flutter app does not
  need it. This app does: `flutter_secure_storage_windows`, which holds the device token,
  includes `atlstr.h`. Without it the build dies with `error C1083` most of the way
  through, long after doctor has said everything is fine.
- **Windows Developer Mode**, for the symlink support Flutter plugins need —
  `start ms-settings:developers`.

**The manifest ships under `hub/`, not under `app/`.** The hub's self-updater mirrors the
`hub/` directory and nothing else, so a manifest beside these sources would never reach an
installed hub — the same shape as the 1.27.x bug where `packages.py` was left out of the
runtime file list and the hub died on import. See `hub/clientrelease.py`.

## Android / iOS

Not built. **Most of this codebase is already platform-neutral** — models, state, the API
client, pairing, the updater, and every screen — but three of the plugins `main.dart`
depends on (`window_manager`, `tray_manager`, `local_notifier`) ship no Android code at
all, because a phone has no window to manage and no tray to sit in. `notify.dart` needs a
`flutter_local_notifications` counterpart, the navigation rail needs to become a bottom
bar, and a release needs an Android keystore.

The decisive one is **background alerts**: Android kills background polling, so without
FCM an Android build only shows alerts while it is open — which is most of the reason to
want it on a phone. Push is therefore a prerequisite for the Android build being useful,
not a later refinement.

`ROADMAP.MD` #11 scopes all of it, including the `push_kind` / `push_token` columns already
waiting on `api_tokens` and why the signed manifest's `builds[]` list means adding a
platform costs no hub or console change.

## Localization

**v1 is English only**, but no string is written inline in a widget — every one lives in
`lib/strings.dart`. That is the whole v1 cost, and it is what makes v2 an asset drop rather
than a sweep through every screen.

**v2 ships the catalogs inside the app**, generated from `hub/locales/{en,de,es}.json` and
switchable at runtime from the app's own settings. Bundled rather than fetched, because
this app gets opened in a car park on a phone with no signal, and a UI whose labels depend
on a round-trip is a UI that is sometimes blank. Generate the assets in the build rather
than hand-copying them, and add a test asserting the key sets agree — the same shape as the
hub's `tests/test_i18n.py`.

## Layout

```
lib/
  main.dart          window, tray, close-to-tray, theme
  version.dart       the running version -- source of truth for a release
  models.dart        tolerant decoders over the hub's JSON
  state.dart         riverpod: session, pollers, alert delta
  notify.dart        Windows toasts, and when NOT to raise one
  strings.dart       every user-facing string, in one place
  api/
    hub_client.dart  bearer auth, timeouts, 401 -> re-pair
    fleet_api.dart   typed calls over existing hub endpoints
  auth/
    pairing.dart     the loopback flow, and its refusals
    token_store.dart secure storage
  update/
    updater.dart     signed manifest -> "is there a newer one?"
  ui/                fleet, alerts, wake, commands, settings, update prompt
test/                models, pairing, updater (real Ed25519), notify, session wiring
windows/runner/      the five tracked runner files; everything else there is generated
  main.cpp           entry point -- claims the instance mutex before anything else runs
  single_instance.*  one client per logon session, and how a second launch wakes it
  Runner.rc          the version resource Explorer shows on the exe
  CMakeLists.txt     names single_instance.cpp in the build
```
