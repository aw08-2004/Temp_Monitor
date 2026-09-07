# FleetHub Android agent

An Android counterpart to `agent/` — the C#/.NET Windows Service on every managed PC — and a
sibling to `agent-linux/`. Same hub, same wire protocol, same house rules; a much smaller
feature set, and unlike the other two, most of what is missing can never be added.

**Status: early, but it has now run.** It compiles to a release-signed APK, its protocol half
has 93 tests, and it has enrolled, reported telemetry and taken a command on a real device
(Samsung Galaxy A15, Android 16). Nothing has been released and no fleet runs it. What is
verified, and what still is not, is stated exactly in *Build, test, run*.

It reports telemetry, enrolls, heartbeats, and executes one command type. See *What it does not
do*.

---

## Why C#, and why two projects

The language question was settled by `agent-linux/README.md` and the answer has not changed:
the hard part of an agent is not the platform code, it is the *protocol* — enrollment identity
that must survive a restart or the fleet gains a duplicate, an offline buffer that backfills
history without lying about timestamps, poll holding that must stay inside the hub's 90-second
offline window. Kotlin would be the idiomatic choice for an Android app and would duplicate the
most dangerous code in the fleet in a dialect nobody here maintains. .NET targets Android with
`net10.0-android`, so that code is *copied* rather than *rewritten*, and a reviewer can diff
this agent's `FleetClient.cs` against the Linux one line for line.

**The split into `FleetHubAgent.Core` and `FleetHubAgent.Android` is the one structural thing
this agent does that the Linux agent does not, and it is forced rather than chosen.** A
`net10.0-android` project cannot be referenced by an xunit project running on a Windows
workstation: it needs the Android workload to restore and a device or an emulator to run a test
on. Had the protocol lived in the Android project, every test of it would have required
hardware, and in practice that means it would have had no tests. So:

| | |
|---|---|
| `src/FleetHubAgent.Core` | Plain `net10.0`. The protocol, the loops, the version gate, machine naming, the dispatcher, `rename`. **Builds and tests on any workstation with the .NET SDK and nothing else.** |
| `src/FleetHubAgent.Android` | `net10.0-android`. The foreground service, the sensor readers, the identity reader, managed configuration, the setup screen. Needs the workload. |

This is **not** the shared core `agent-linux/README.md` rejects. That rejection is about
reaching into `agent/`, which is installed on every managed Windows PC and self-updates from a
signed manifest; getting a refactor of that project wrong means a fleet that silently stops
updating. Nothing here touches it. The wire shapes are still duplicated from the other agents
rather than shared with them, for exactly the reason that file gives.

## The version number is a safety mechanism

`AgentConfig.Version` is `0.1.0`, and it must stay below `3.0.0`. The hub's
`AGENT_TRAIN_MIN_VERSION` is what makes this work, and three separate things key off it:

| Below 3.0.0 | What that buys |
|---|---|
| `get_advertised_version()` returns nothing | The hub never points this agent at the **Windows** agent's signed manifest — a `win-x64` binary an Android device cannot execute at all |
| Every `MIN_*_AGENT` gate in `hub/static/js` reads it as too old | The console does not offer a phone a terminal, a process list or a file browser. Unlike on Linux, these are not merely unimplemented: the platform forbids them, so there is no future version that could answer |
| `agents_outdated` skips sub-3.0 machines | A shelf of tablets does not read as permanently "behind" on a release train it is not on |

This is the **fifth** version line in the repo — hub, agent, client, agent-linux, agent-android —
and it has **three** files to keep in step rather than the usual two: `AgentConfig.Version`, the
Core csproj's `<Version>`, and the Android csproj's `<ApplicationDisplayVersion>` (which is what
the phone's own app-info screen shows). `VersionGateTests` holds all three together, and
deliberately does *not* tie `<ApplicationVersion>` to any of them — that is the integer Android
sorts upgrades by, a build counter with no relationship to a dotted version.

**No hub change was needed to support this agent.** That is the property the whole design rests
on, and it survived contact with Android intact.

## What it does

| | |
|---|---|
| **Telemetry** | Temperature, memory, per-volume storage, battery charge and voltage, and CPU load where the device allows it. Flattened into the sensor shape `extract_diagnostics` already reads (`/cpu/0`, `/ram`, `/volume/...`), so the machine page populates with no hub change |
| **Identity** | `Build.MODEL` / `MANUFACTURER` for model and vendor, `Build.VERSION.RELEASE` for the OS caption, `Build.DISPLAY` as `os_build`, the primary ABI as `os_arch`, and the SSAID as `serial_number` — see below |
| **Enrollment** | The hub's shared `AGENT_ENROLLMENT_SECRET`, from an MDM's managed configuration or typed once on the setup screen |
| **Offline buffer** | Bounded at 1000 sensor-stripped reports, flushed oldest-first on reconnect. Earns its keep here more than anywhere: a phone leaves the network several times a day |
| **Commands** | `rename`, `locate_device` |
| **Capabilities** | The heartbeat states the platform and the command types this agent implements, so the hub stops queueing work it can never perform. Derived from the dispatcher, not written out -- see below |

Three concurrent loops — telemetry, heartbeat, commands — for the reason the Windows agent's six
exist: in a serial loop the slowest step sets the latency of every other one.

### Temperature is battery temperature, usually

Every other agent reports a CPU temperature, and the hub's fleet chart, its alert rules and its
history are all built on that one number. An ordinary Android app usually cannot read one:
`/sys/class/thermal` is blocked by SELinux on most modern devices and
`HardwarePropertiesManager` is system-only. So the reader tries the thermal zones and falls back
to **battery temperature**, which every device with a battery reports and which needs no
permission.

That fallback is a real measurement rather than a stand-in. A phone in a hot van, a tablet
charging in a sealed enclosure and a device with a failing cell are exactly the fleet-visible
problems a temperature chart should catch, and all three show up in battery temperature. It is
not a CPU temperature, so the sensor it emits is labelled `Battery` and the machine page shows
it as such; the agent logs once, at startup, which source it is charting.

### `serial_number` is not a serial number

`Build.getSerial()` needs `READ_PRIVILEGED_PHONE_STATE`, which is signature-level: only a system
app or one signed with the platform key can hold it. **A normal build cannot read a hardware
serial on any device, at any permission level, since Android 10.** So the field carries
`Settings.Secure.ANDROID_ID` (the SSAID), which is unique per device, per app-signing key, per
user, and survives reboots and reinstalls.

That buys exactly what the hub needs it for — `resolve_serial_group()` can merge a renamed
device with the machine it used to be. It is **not** the number on the chassis, not what the
asset register calls this device, and it changes on a factory reset.

**It also changes if the APK is ever re-signed with a different key, and that is an operational
hazard rather than a detail: every device in the fleet would look like a brand new machine at
once.** The Android signing keystore is therefore fleet state, to be kept with
`BACKUP_MASTER_KEY` and the Ed25519 release key — not build configuration to be regenerated.

Known junk values are filtered (`IdentityCleaning.CleanStableId`), notably `9774d56d682e549c`,
which a long-standing Android defect returned as the SSAID on a large number of devices — a
constant shared across unrelated hardware, which the hub's duplicate-serial merge would collapse
into one machine.

### `rename` changes what the device reports, not its hostname

Android has no hostname an app may set. `rename` changes the name the agent **sends**, stored on
the device, and the result says so in as many words — nothing else on the phone changes, and
Settings still shows the old device name.

The dangerous half is the same as on Linux: the hub keys a machine on the name it reports, so a
renamed device arrives as a machine the hub has never seen, and what collapses the two is
`resolve_serial_group()` matching on `serial_number`. A device with no usable SSAID would rename
itself into a permanent duplicate, so `RenameExecutor` **refuses** in that state rather than
renaming and warning. An operator reads a result once; the duplicate outlives the reading.

Devices also default to a name that cannot collide — the model plus eight characters of the
stable id (`Pixel-Tablet-5b4a3210`). That is not cosmetic. `Build.MODEL` is a marketing name
shared by every unit of a model, so thirty identical tablets reporting `Pixel Tablet` would not
appear as thirty confusingly-named machines: they would appear as **one**, overwriting each
other's telemetry several times a second, with nothing anywhere reporting an error.

## What it does not do

Most of the Windows agent's command set, and — unlike the Linux agent's gap — most of it is a
platform limit rather than a backlog. `CommandDispatcher` distinguishes the two in the result it
returns, because "not implemented" sends an operator looking for a version that will do it.

**Cannot be done by an ordinary app, at any version:**

| | |
|---|---|
| `restart` | No reboot API. A device enrolled as device-owner through an MDM can be rebooted *by that MDM* |
| `shutdown` | No power-off API, not even for a device owner |
| `run_script` | No shell. An app may only execute its own code, in its own sandbox, as its own unprivileged user |
| `list_directory`, file transfer | An app sees its own storage and what the user has granted, not the device's filesystem |
| process list, `kill_process` | An app cannot see another app's processes; true since Android 8 |
| remote view/control (`#2`) | Screen capture requires the person holding the device to accept a system prompt *per session*, so unattended remote view is not possible |

**Could be done, and is not yet** — in rough order of what would be worth doing next:

1. **`show_message`.** The one command where a phone is *better* than a PC, and the obvious next
   one. It needs the full contract, not a notification: buttons with hub-supplied ids, a JSON
   result carrying `outcome`/`shown_at`/`responded_at`, and the `no_session` case. The hub's
   `rules.py` routes on that outcome, so a half-implementation strands the routing.
2. **Push instead of polling.** FCM against the `push_*` columns already on `api_tokens` would
   let the command loop idle instead of holding a request every 25 seconds, which is most of
   this agent's battery cost. Roadmap #11 scopes the same work for the client.
3. **Patch inventory** — the installed-app list and their versions. A Play policy problem
   (`QUERY_ALL_PACKAGES`) before it is a technical one.
4. **Signed self-update.** The Windows agent's Ed25519 manifest verification does *not* port:
   Android installs APKs through the package installer, and an app cannot silently update
   itself unless it is a device owner. For a managed fleet the honest answer is that the MDM
   ships the APK.
5. Network throughput and per-app data; GPU sensors.

### Scheduled commands arrive anyway

**The console's `MIN_*_AGENT` gates do not protect this agent, and assuming they did was wrong.**
Those gates are JavaScript in the console: they stop an operator being offered a button. Nothing
runs them for a command the hub's own scheduler or rules engine dispatches, because those target
a machine *set* rather than a button. `hub/fleet.py`'s `SCHEDULED_COMMANDS` — `backup_files`,
`restore_files`, `install_patches`, `deploy_package` — reach an Android device regardless.

The first phone to enroll was inside a fleet-wide backup profile and had `backup_files` queued
to it within a minute. It answered, correctly, that it could not run it.

So those types are in `CommandDispatcher`'s impossible table with a reason and, where there is
one, the action to take. That message is now the fallback rather than the mechanism: **the hub
fix shipped in 1.98.0.**

### The capability report is what actually stops them

The heartbeat carries a `capabilities` block -- `{"platform": "android", "commands": [...],
"features": [...]}` -- and the hub refuses any command type a machine's list leaves out. The
four schedulers filter their targets against it first, so a phone in a nightly backup profile is
no longer aimed at rather than collecting a failed run every night.

Two things about it matter on this side:

- **The command list is derived from `CommandDispatcher.Implemented`, never written out.** A
  hand-kept copy drifts the first time an executor is added, and it drifts the way that hurts:
  the hub goes on refusing a command this agent has just learned to run, and the only symptom is
  a console button that stays grey.
- **It is sent on every heartbeat, not change-only.** A hundred bytes every ten seconds buys a
  report that is self-healing: the hub writes only when the content differs, so a hub restored
  from a database backup re-learns the fleet without anybody reinstalling an app. An agent
  holding its own "already sent" flag would stay silent instead.

`AgentCapabilities` also names the feature slugs the later phases will report (`locate`,
`app_policy`, `time_policy`, `device_owner`) so each is added in one place rather than as a
string the hub has never heard of and silently ignores. None is reported today.

This is also what lets `AgentConfig.Version` stay at `0.1.0` permanently: the version identifies
a release train, capabilities describe a feature set, and the console no longer has to infer the
second from the first.

The Dashboard's `_OS_MATCHES` gained an `android` bucket in the same release, ordered before
`linux` -- an Android device really is running a Linux kernel, and a caption mentioning both
must not file phones in with the servers.

## Locating a device

`locate_device` asks the device where it is, once, when an operator presses a button. Nothing
polls and there is no background collection.

**"I could not tell you where I am" is a SUCCESS.** A device indoors, with location switched
off, or with the permission never granted has answered the question truthfully. Reported as a
failure it would be indistinguishable in the console from a network problem or a crashed agent,
and an operator hunting a lost phone would spend their time on the agent. A `Fail` from this
executor means one thing only: it could not run.

**The device tells the person holding it who asked** -- a notification naming the operator, on
its own channel at Default importance rather than the silent one the foreground service uses,
posted BEFORE the fix is taken and whether or not one is found. A notification only on success
would make a failed locate the quiet way to check whether somebody's phone is switched on. It is
also never awaited: a device where the notification permission was refused must still answer, or
refusing the notification would become a way to refuse being found.

**Framework `LocationManager`, not `FusedLocationProviderClient`.** The fused provider is
better indoors and would be the first Google dependency in this repo: a large transitive binding
graph plus a hard requirement that Play Services is present and current, which fails on
de-Googled builds and on exactly the cheap tablets a fleet buys by the dozen.

**Both providers are asked at once and the best answer within the budget wins.** Taking the
first fix would always take the network one -- about a second, with a radius of several hundred
metres -- while GPS takes tens of seconds and lands inside ten. A fix at 25 m or better ends the
wait early; otherwise the budget runs out and whatever was collected is the answer. A last known
position is returned rather than nothing, flagged `stale` and carrying the time it was actually
taken.

**Background access comes from the foreground service, not from a background permission.**
Android 10+ blocks location while an app is not visible unless it holds
`ACCESS_BACKGROUND_LOCATION` -- a standing grant to follow a device -- or is running a
foreground service started with the `location` type. The agent promotes its service to include
that type for the length of one request and demotes immediately after. The permission is
checked BEFORE the promotion: from Android 14, starting a foreground service with the location
type while lacking it throws, which would take down the whole agent for the crime of being
asked where it is on a device where somebody said no.

Everything about what a fix MEANS -- the time budget and its bounds, what counts as a fix, the
wire shape, what to say when there is not one -- is in `FleetHubAgent.Core`, where it has 15
tests that run on a workstation. The two Android classes only answer "here is a position, or
here is why not" and "tell the person who asked".

## Fully managed (device owner)

The agent can hold **device owner** on a device provisioned by QR at its setup wizard, which is
what makes the rest of roadmap #23 possible: an ordinary Android app cannot locate a device
silently, suspend another app, or lock a screen. The hub draws the code (see
`hub/provisioning.py`); nothing in this app can start the flow.

Three components exist for it, and all three are mandatory from API 29. A DPC missing any of
them does not provision at all -- and by the time that is discovered the device has already
been factory reset, so the cost of the mistake is a second wipe:

| Component | Why |
|---|---|
| `FleetDeviceAdminReceiver` | The admin component the QR names. Its class name is **pinned explicitly** rather than left to .NET Android's generated `crc64...` name, whose hash can change between builds -- a printed QR naming a component no build carries is a wipe with no way back |
| `GetProvisioningModeActivity` | Answers the setup wizard's "what kind of management?" with fully-managed-device. Refuses a wizard offering only a work profile, with the reason in logcat |
| `PolicyComplianceActivity` | The DPC's chance to apply policy and say it is satisfied. Returns `Result.Ok` no matter what: a throw here strands the device in the setup wizard, and the only way out of that is another factory reset |

**The degradation contract.** Everything that needs device ownership calls
`DeviceOwner.IsManaged` first and degrades to "this device is not fully managed" rather than
throwing. Without it, a sideloaded build answers a lock or a suspend with a SecurityException
that the dispatcher turns into "executor error: ...", which in the console reads exactly like
broken hardware.

**One power is taken today**, `setUserControlDisabledPackages`, which stops a person
force-stopping the agent from the app info screen. It is **not** a fix for the OEM
power-manager problem: those killers run inside the system and do not go through that path.
`device_admin.xml` declares three more (`force-lock`, `limit-password`, `reset-password`) plus
`wipe-data` before any is used, because a device owner cannot be re-prompted -- a policy
discovered missing later may cost another factory reset to add.

**The provisioning QR is fleet state, alongside the keystore.** It carries the fleet's shared
enrollment secret so that one scan both provisions and enrols; a photograph of it is that
secret. It is also bound to the signing key by the signature checksum in it, so re-signing the
APK invalidates every printed copy -- the same key whose SSAID is what every device reports as
its serial number.

## Layout

```
src/FleetHubAgent.Core/       the protocol -- plain net10.0, tested on the workstation
  AgentConfig.cs              version, endpoints, cadence
  AgentLoops.cs               the three loops
  MachineNaming.cs            the derived name, and why it cannot be the model alone
  MachineNameProvider.cs      the current name, and persist-before-adopt
  Fleet/                      hub client, dispatcher, capability report, rename, locate
  State/                      the identity store, behind an interface
  Telemetry/                  the report builder, the sensor contracts, identity cleaning
src/FleetHubAgent.Android/    the app -- net10.0-android, needs the workload
  AgentService.cs             the foreground service, and the composition root
  MainActivity.cs             the setup and diagnostics screen
  BootReceiver.cs             coming back after a reboot
  Platform/                   SharedPreferences, Build fields, sensors, location, managed config, logcat
  Policy/                     the device-admin component and the provisioning activities
  Properties/AndroidManifest.xml   permissions and application settings only -- see the note in it
  Resources/xml/app_restrictions.xml   the keys an MDM can push
  Resources/xml/device_admin.xml       the policies the admin component declares
tests/FleetHubAgent.Core.Tests/  xunit; everything under test is pure
```

Class-suffix conventions carry over from `agent/`: `*Executor.cs` implements `ICommandExecutor`
for one fleet command type and its `Type` must match the hub's `COMMAND_TYPE`. File-scoped
namespaces, primary constructors, `sealed` by default, `--` rather than em dashes.

## Build, test, run

The protocol half needs nothing but the .NET SDK, and this is the command that matters:

```bash
dotnet test agent-android/tests/FleetHubAgent.Core.Tests/FleetHubAgent.Core.Tests.csproj
```

The Android half needs the workload, an Android SDK and **a JDK the build can actually use**.
On a machine that has none of them:

```bash
dotnet workload install android
```

```bash
dotnet build agent-android/src/FleetHubAgent.Android/FleetHubAgent.Android.csproj -t:InstallAndroidDependencies -p:AndroidSdkDirectory="$env:LOCALAPPDATA\Android\Sdk" -p:JavaSdkDirectory="$env:LOCALAPPDATA\Android\jdk" -p:AcceptAndroidSDKLicenses=True
```

That second command downloads the SDK (platform 36, build-tools, platform-tools) **and a
Microsoft OpenJDK 17**, and accepts the Android SDK licences. The JDK is not optional
housekeeping: this workstation has Oracle JDK 24 on `PATH`, .NET Android 36 cannot parse its
version, and the failure is a `warning XA0034` followed by a `NullReferenceException` inside
`ResolveAndroidTooling` — which reads like a broken SDK install rather than a JDK that is too
new. Point `JavaSdkDirectory` at the provisioned 17 and it goes away.

Both paths must then be passed on every build, because nothing on this machine records them:

```bash
dotnet publish agent-android/src/FleetHubAgent.Android/FleetHubAgent.Android.csproj -c Release -p:AndroidSdkDirectory="$env:LOCALAPPDATA\Android\Sdk" -p:JavaSdkDirectory="$env:LOCALAPPDATA\Android\jdk"
```

They are **deliberately not written into the csproj**: they are one workstation's paths, and a
committed absolute path is a build that works for whoever added it and breaks for everyone else.
Setting `ANDROID_HOME` and `JAVA_HOME` in the environment is the per-machine answer.

### What is and is not verified

| | |
|---|---|
| `FleetHubAgent.Core` | **Tested.** 88 xunit tests, no SDK required |
| `FleetHubAgent.Android` | **Compiles**, Debug and Release, no warnings. Produces a signed 9.7 MB APK |
| The merged manifest | **Checked** with `aapt2 dump`: `minSdk` 26 / `target` 36, `allowBackup=false`, the service's `foregroundServiceType` carrying both `specialUse` and `dataSync`, and all four boot actions on the receiver |
| On real hardware | **Enrollment, telemetry ingest and the foreground service, on a Galaxy A15 / Android 16.** Battery temperature reads; all 26 thermal zones are denied even to `adb shell`; the service runs as `specialUse`; identity and machine name survive a reinstall |
| Still unverified | Anything time-dependent: whether the service survives an OEM power manager overnight, whether the boot receiver fires on this vendor's skin, and every command except `rename` |

**The first real device changed the design twice**, which is the argument for doing this before
a fleet rollout rather than after. It showed that the notification status line could not
distinguish "reporting fine, waiting for its secret" from "cannot reach the hub" — the exact
question during a fresh install. And it received a `backup_files` command within a minute of
enrolling, disproving the assumption that the console's version gates keep unsupported commands
away. See *Scheduled commands arrive anyway* below.

The agent logs to **logcat** under the tag `FleetHubAgent`:

```bash
adb logcat -s FleetHubAgent:*
```

Not a file, unlike the Windows agent's rolling log. logcat is the platform's own ring buffer —
`adb` reads it and every bug report includes it — and a file under the app's data directory
would be a second copy that nothing rotates and no support tool collects. The cost is that a
device's agent history is measured in minutes on a chatty phone; for "why did this tablet stop
reporting on Tuesday", the answer has to come from the hub's side.

## Installing

There is no `curl | bash` equivalent, and there cannot be: Android installs applications, not
binaries. Two supported paths.

**By MDM, which is the one that scales.** Push the APK, then push a managed configuration
carrying `hub_url` and `enrollment_secret` (the keys are in
`Resources/xml/app_restrictions.xml`, and an MDM renders them as fields in its own console).
The device enrolls itself with nobody touching it. `machine_name` and `asset_tag` can be pushed
too; `machine_name` applies **only** to a device still on its derived default, so an MDM policy
never silently undoes a rename issued from the console.

**By hand, for a pilot device.** Sideload the APK, open the app once, and enter the hub URL and
the enrollment secret. `--secret`'s counterpart is that one field.

`enrollment_secret` is the hub's **`AGENT_ENROLLMENT_SECRET`** — one shared value for the whole
fleet, from the hub's `.env`, printed once by `install.ps1` when the hub was set up. It is not
shown anywhere in the console. Without it a device installs cleanly, comes up online, charts and
reports its inventory — and never accepts a command. That state is easy to miss and there is no
installer output to warn about it, so the setup screen says so in as many words.

### Two things will stop a device reporting, and neither is a bug

- **Battery optimisation.** Android and several manufacturers' own power managers freeze a
  background app after a few hours idle; the device then reads offline in the console until
  somebody picks it up. The setup screen has a button that opens the system's battery screen,
  because an app cannot grant itself the exemption. On a fleet deployment, the MDM should be
  granting it.
- **A missing notification permission** (Android 13+) does *not* stop the agent — but it hides
  the foreground notification, which is the only status display the device has.

### Signing, and why the keystore is fleet state

**There is no certificate authority in this story.** An Android release key is self-signed and
free — unlike the Windows code-signing certificate roadmap #11 is waiting on, nothing is bought
and nothing is verified by a third party. Make one with the JDK the build provisioned:

```bash
keytool -genkeypair -v -keystore fleethub-agent.keystore -storetype PKCS12 -alias fleethub -keyalg RSA -keysize 4096 -validity 10000
```

`-validity 10000` is about 27 years; an expired key cannot sign an update and there is no
renewal that keeps the same identity, so err long. PKCS12 keeps one password for the store and
the key.

Then copy `signing.props.example` to `signing.props` (gitignored) and fill it in. Absent, the
build falls back to the SDK's debug keystore, which is what keeps `dotnet build` working for
anyone who only wants to compile.

**Losing that keystore costs two things, and neither is recoverable:**

- No installed agent can be updated in place. Android refuses an APK signed with a different
  key, so every device needs an uninstall by hand.
- Every device's SSAID changes, because it is derived per app-signing key. The whole Android
  fleet re-appears in the console as new machines at once, and the duplicate-serial merge
  cannot put them back together — it matches on the very value that changed.

So it belongs wherever `BACKUP_MASTER_KEY` is kept, and it moves across a server migration
before anything else.

**Which is also why the keystore comes before the first install, not after.** A device that
runs a debug-signed build and later a release-signed one reports two different serial numbers
and appears as two machines, and the switch needs an uninstall anyway. Generate the key first
and sign even the pilot build with it.

### Releases

None yet. When there is one, the shape to follow is `agent-linux`'s: a tag prefix and an asset
name that whatever fetches it matches on exactly. Unlike the Windows agent there is **no signed
manifest and no self-update** — an app cannot silently update itself on Android unless it is a
device owner — so the trust root for an APK is the keystore above plus however the MDM delivers
it.

## Running unprivileged

It does, and there is no alternative to discuss. The Linux agent runs as root and its unit file
argues the case at length; on Android an app runs as its own unprivileged UID and *cannot* be
anything else. Every limitation in *What it does not do* above follows from that, and the
containment that matters is the same one that matters everywhere else in this fleet: at the hub,
where `ALLOWED_EMAILS` plus permission groups decide who may send a command at all.
