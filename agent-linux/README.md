# FleetHub Linux agent

A Linux counterpart to `agent/` — the C#/.NET Windows Service on every managed PC. Same hub,
same wire protocol, same house rules; a much smaller feature set, on purpose.

**Status: early. Nothing has been released, and nothing in the field runs this.** It reports
telemetry, enrolls, heartbeats, and executes four command types. See *What it does not do*.

---

## Why C#, and why a separate project

The obvious alternatives were a Python daemon (the hub is Python) or a fresh Go binary. Both
were rejected for the same reason: the hard part of an agent is not the platform code, it is
the *protocol* — enrollment identity that must survive a restart or the fleet gains a
duplicate, an offline buffer that backfills history without lying about timestamps, poll
holding that must stay inside the hub's 90-second offline window. All of that is already
written, already reasoned about in comments, and already deployed. Rewriting it in another
language would duplicate the most dangerous code in the fleet in a dialect nobody here
maintains.

.NET publishes a self-contained single-file `linux-x64` binary, so a managed box needs no
runtime installed — the same deployment shape as the Windows agent, which is what keeps the
install story one story.

**The code is a separate project rather than a shared core, and that is deliberate.**
Extracting `TempMonitorAgent.Core` would mean editing the csproj and moving files of an agent
installed on every managed Windows PC that self-updates from a signed manifest. The cost of
getting that wrong is a fleet that silently stops updating — exactly the failure CLAUDE.md's
versioning rules exist to prevent. So this starts standalone and earns a shared core once it
has a release train of its own. What keeps the duplicated wire shapes honest in the meantime is
the hub: the field names are the hub's, so drift shows up as a machine reporting nothing rather
than as a silent disagreement.

## The version number is a safety mechanism

`AgentConfig.Version` is `0.1.0`, and it must stay below `3.0.0`. The hub's
`AGENT_TRAIN_MIN_VERSION` is what makes this work, and three separate things key off it:

| Below 3.0.0 | What that buys |
|---|---|
| `get_advertised_version()` returns nothing | The hub never points this agent at the **Windows** agent's signed manifest — a `win-x64` binary it would have no idea what to do with |
| Every `MIN_*_AGENT` gate in `hub/static/js` reads it as too old | The console does not offer a Linux machine a terminal, a process list or a file browser this agent cannot answer |
| `agents_outdated` skips sub-3.0 machines | A Linux fleet does not read as permanently "behind" on a release train it is not on |

The compatibility API the Windows agent's MINOR feeds does the right thing here for free. This
is enforced by `VersionGateTests`, not just documented — a well-meaning "bring the version in
line with the other components" change would break all three at once, silently.

**No hub change was needed to support this agent.** That is the property the whole design rests
on.

## What it does

| | |
|---|---|
| **Telemetry** | CPU temperature, CPU load and clock, memory, per-volume disk usage — from `/sys/class/hwmon`, `/sys/class/thermal`, `/proc/stat`, `/proc/cpuinfo`, `/proc/meminfo`. Flattened into the sensor shape the hub's `extract_diagnostics` already reads, so the machine page populates with no hub change |
| **Identity** | DMI (`/sys/class/dmi/id`) for serial, model, manufacturer, asset tag; `/etc/os-release` for the OS caption; kernel release as `os_build` |
| **Enrollment** | The hub's shared `AGENT_ENROLLMENT_SECRET`, from `/etc/fleethub/agent.secret` (0600, permissions *checked*, not assumed) or the env var of the same name |
| **Offline buffer** | Bounded at 1000 sensor-stripped reports, flushed oldest-first on reconnect |
| **Commands** | `restart`, `shutdown`, `rename`, `run_script` |

Three concurrent loops — telemetry, heartbeat, commands — for the reason the Windows agent's
six exist: in a serial loop the slowest step sets the latency of every other one.

## What it does not do

Everything else, and none of it is reachable from the console anyway (see the version table
above). In rough order of what would be worth doing next:

1. **Live command output streaming** (`MIN_STREAMING_AGENT` 3.1.0) — the endpoint and the
   sequencing already exist on the hub; `onOutput` is threaded through the executors ready for
   it.
2. **Signed self-update.** The Windows agent's Ed25519 manifest verification ports one-to-one,
   but it needs its own manifest, its own channel and a `release.sh` that moves the two-file
   version pair. Until that exists, an upgrade is `install/install.sh` run again. **Do not point
   this agent at the Windows manifest.**
3. **Patch inventory** (`apt`/`dnf` — roadmap #14's Linux half).
4. **Process list** (`MIN_PROCESS_AGENT` 3.24.0) — `/proc` walk, demand-driven like the Windows
   one.
5. **PTY terminal** (`MIN_PTY_AGENT` 3.15.0) — `forkpty` instead of ConPTY.
6. GPU and fan sensors; remote view/control (`#2`) is a long way off and may never be worth it.

Two things need a **hub** change and are recorded in `ROADMAP.MD` #22 rather than worked around
here:

- `run_script`'s `shell` enum is `("powershell", "cmd")`. This agent runs the script under
  `bash`/`sh` regardless and says so in the first line of the result, because refusing would
  make `run_script` permanently unusable. Adding `bash`/`sh` to the enum is the fix.
- `_OS_MATCHES` buckets `ubuntu`/`debian`/`rhel`/`fedora`/`suse`/`alma`/`rocky` and the bare
  word `linux`, so a `PRETTY_NAME` like `Pop!_OS 22.04 LTS` buckets as *unknown*. The agent
  reports the caption honestly rather than smuggling the word "Linux" into it.

## Layout

```
src/FleetHubAgent/        the agent
  AgentConfig.cs          version, endpoints, cadence, state paths
  Worker.cs               the three loops
  Fleet/                  hub client, dispatcher, executors
  Telemetry/              /proc and /sys readers, the report builder
  State/                  agent.json, and keeping it root-only
tests/FleetHubAgent.Tests/  xunit; everything under test is pure
packaging/                the systemd unit
install/install.sh        the web installer (curl | sudo bash)
```

Class-suffix conventions carry over from `agent/`: `*Executor.cs` implements `ICommandExecutor`
for one fleet command type and its `Type` must match the hub's `COMMAND_TYPE`. File-scoped
namespaces, primary constructors, `sealed` by default, `--` rather than em dashes.

## Build, test, run

```bash
dotnet test agent-linux/FleetHubAgent.slnx
dotnet publish agent-linux/src/FleetHubAgent/FleetHubAgent.csproj -c Release -o agent-linux/dist
```

The publish cross-compiles from Windows; the RID is pinned in the csproj so a bare `dotnet
publish` produces the real artifact (~72 MB, self-contained, single file). The test project
clears that RID so tests run on the workstation.

The agent logs to stdout only — systemd journals, rotates and expires it. The Windows agent's
rolling file log exists because Windows has nowhere to put a service's stdout.

## Installing

The Linux counterpart of `irm .../install.ps1 | iex`:

```bash
curl -fsSL https://raw.githubusercontent.com/aw08-2004/Temp_Monitor/main/agent-linux/install/install.sh | sudo bash -s -- --secret 'THE-SECRET'
```

`--secret` is the hub's **`AGENT_ENROLLMENT_SECRET`** — one shared value for the whole fleet,
from the hub's `.env`, printed once by `install.ps1` when the hub was set up. It is not shown
anywhere in the console. Without it the agent reports telemetry but takes no commands, and says
so in its log.

An argument is visible in `ps` while the installer runs, so for anything but a one-off prefer:

```bash
curl -fsSL .../install.sh | sudo FLEETHUB_ENROLLMENT_SECRET='THE-SECRET' bash
# or
curl -fsSL .../install.sh | sudo bash -s -- --secret-file /root/.fleethub-secret
```

Other options: `--hub URL` to override the compiled-in hub, `--agent-url URL` for an internal
mirror (or when a lot of machines behind one NAT would hit GitHub's 60/hour unauthenticated API
limit), `--binary PATH` to install a locally built file with no download at all, `--unit PATH`
for a local checkout's unit file, and `--uninstall`.

It resolves the binary exactly as `install.ps1` does — newest GitHub release tagged
`linux-agent-v*`, asset named `fleethub-agent` — checks the architecture before it stops
anything, downloads before it stops the running agent, verifies the download is really an ELF
binary rather than a proxy's error page, and confirms `systemctl is-active` afterwards rather
than trusting `enable --now`'s exit code.

### It needs a release to exist first

**There is no `linux-agent-v*` release yet**, so the one-liner above will stop with a message
saying so. Until one is cut, install from a local build:

```bash
dotnet publish src/FleetHubAgent/FleetHubAgent.csproj -c Release -o dist
scp dist/fleethub-agent user@target:/tmp/
ssh user@target 'curl -fsSL .../install.sh | sudo bash -s -- --binary /tmp/fleethub-agent --secret "..."'
```

To cut the release the installer expects — note the tag prefix and the asset name are what
`install.sh` matches on, so both must be exact:

```bash
dotnet publish src/FleetHubAgent/FleetHubAgent.csproj -c Release -o dist
gh release create linux-agent-v0.1.0 dist/fleethub-agent \
  --title "Linux agent v0.1.0" --notes "First cut. Unsigned, untested on real hardware."
```

Unlike the Windows agent there is **no signed manifest and no self-update**, so this release is
only ever read by `install.sh` over HTTPS — the trust root is GitHub plus TLS, not the fleet's
Ed25519 key. That is the main reason not to widen this beyond a pilot machine yet; see
*What it does not do* above.

## Running as root

It does, and the unit file says why in full. Briefly: `/sys/class/dmi/id/product_serial` is
`0400`, and that serial is how the hub collapses duplicate machines — an unprivileged agent
renames a machine into a permanent duplicate. `restart`, `shutdown` and `run_script` are root
operations by definition. A capabilities-only user would be root with extra steps and a false
sense of containment; the containment that matters is at the hub, where `ALLOWED_EMAILS` plus
permission groups decide who may send a command at all.
