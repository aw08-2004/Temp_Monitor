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

Four concurrent loops — telemetry, heartbeat, commands, updates — for the reason the Windows
agent's six exist: in a serial loop the slowest step sets the latency of every other one.

## Self-update

Signed, and verified against an **Ed25519 key held offline — the same key the Windows fleet
uses.** One release trust root for the product, two manifests. The hub is deliberately not part
of it: a compromised hub, or anyone able to answer for `raw.githubusercontent.com`, still cannot
put a binary on a Linux machine.

The sequence is the Windows agent's, in the order that matters:

1. Fetch `agent-linux/agent.manifest.json` and its detached `.sig` from **`main`** — a branch,
   not a release, because this is the one URL that must keep working for a fleet to be
   reachable, and a branch cannot be retagged or unpublished.
2. Verify the signature over the raw bytes **before parsing them**. Parsing first would mean
   deserializing attacker-controlled JSON to decide whether to trust it.
3. Only if strictly newer — never equal. A republished version number must do nothing.
4. Download, check sha256 against the **signed** value, write, then **re-read from disk and
   hash again**, because what gets installed is the file, not the bytes just checked.
5. `chmod 0700`, rename the running binary to `.old`, rename the new one into place, exit 17.
   systemd (`Restart=always`) brings it back within ten seconds.
6. The `.old` binary **survives the reboot** and is deleted only once the new build has reached
   the hub — a report or a heartbeat the hub accepted. "It started" is not "it works", and the
   gap between them is where a fleet-wide brick lives.

Bounded by a per-target restart guard (`restart_state.json`, three attempts), so a bad build
burns its own budget without spending the budget of the release that fixes it. `FLEETHUB_NO_UPDATE=1`
pins a machine to its current build.

**The check is weekly, and only weekly.** The Windows agent also gets nudged by `/api/report`
answering with `latest_version`; this agent reports a 0.x version, so the hub deliberately tells
it nothing (see the version table above). A Linux fleet therefore converges over a week, not
over fifteen minutes — worth knowing when timing a release.

Two Linux-specific traps the Windows code has no equivalent for, both of which brick a machine
if the port is done blindly:

- **Staging shares a filesystem with the binary** (`/opt/fleethub/agent/.update`, not the state
  root). `File.Move` is an atomic `rename(2)` within one filesystem and a *copy* across two —
  and `/opt` and `/var` are separate mounts on plenty of real installs. A copy interrupted by a
  full disk leaves a partial binary where the agent used to be.
- **The execute bit must be set.** A download is `0644`. Windows has no such concept, so the
  Windows updater has no equivalent line; omit it and systemd reports "Permission denied" for a
  binary that is present, correct and verified.

## What it does not do

Everything else, and none of it is reachable from the console anyway (see the version table
above). In rough order of what would be worth doing next:

1. **Live command output streaming** (`MIN_STREAMING_AGENT` 3.1.0) — the endpoint and the
   sequencing already exist on the hub; `onOutput` is threaded through the executors ready for
   it.
2. **Patch inventory** (`apt`/`dnf` — roadmap #14's Linux half).
3. **Process list** (`MIN_PROCESS_AGENT` 3.24.0) — `/proc` walk, demand-driven like the Windows
   one.
4. **PTY terminal** (`MIN_PTY_AGENT` 3.15.0) — `forkpty` instead of ConPTY.
5. GPU and fan sensors; remote view/control (`#2`) is a long way off and may never be worth it.

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

`curl` or `wget`, whichever the box has — Debian's minimal images and appliance distros built
on them (OpenMediaVault, for one) ship `wget` and no `curl`:

```bash
wget -qO- https://raw.githubusercontent.com/aw08-2004/Temp_Monitor/main/agent-linux/install/install.sh | sudo bash -s -- --secret 'THE-SECRET'
```

If the account is not in sudoers — also common on appliance distros, which manage their own
users and do not make them admins — become root first and drop the `sudo`:

```bash
su -
wget -qO- <same url> | bash -s -- --secret 'THE-SECRET'
```

Note that both halves of a pipeline start regardless, so if the fetch fails you still get a
`sudo` password prompt from the right-hand side. That prompt is not evidence the install ran.

`--secret` is the hub's **`AGENT_ENROLLMENT_SECRET`** — one shared value for the whole fleet,
from the hub's `.env`, printed once by `install.ps1` when the hub was set up. It is not shown
anywhere in the console. Without it the machine installs cleanly, comes up online, charts and
reports its inventory — and never accepts a command. That state is easy to miss, so the
installer names it again as the last thing it prints.

An argument is visible in `ps` while the installer runs, so for anything but a one-off prefer:

```bash
curl -fsSL .../install.sh | sudo AGENT_ENROLLMENT_SECRET='THE-SECRET' bash
# or
curl -fsSL .../install.sh | sudo bash -s -- --secret-file /root/.fleethub-secret
```

The installer reads **either** `AGENT_ENROLLMENT_SECRET` (the name the hub's `.env`, the Windows
agent and this agent's own runtime all use) or `FLEETHUB_ENROLLMENT_SECRET`. Both work, so
whichever one you already have exported is the right one — passing an env var the installer did
not read would otherwise be indistinguishable from passing none, and would leave a machine that
installs cleanly and silently takes no commands.

Other options: `--hub URL` to override the compiled-in hub, `--agent-url URL` for an internal
mirror (or when a lot of machines behind one NAT would hit GitHub's 60/hour unauthenticated API
limit), `--binary PATH` to install a locally built file with no download at all, `--unit PATH`
for a local checkout's unit file, and `--uninstall`.

It resolves the binary exactly as `install.ps1` does — newest GitHub release tagged
`linux-agent-v*`, asset named `fleethub-agent` — checks the architecture before it stops
anything, downloads before it stops the running agent, verifies the download is really an ELF
binary rather than a proxy's error page, and confirms `systemctl is-active` afterwards rather
than trusting `enable --now`'s exit code.

### Releases

`linux-agent-v0.1.0` is published, so the one-liner above resolves. Installing without any
release at all — a local build, or an air-gapped machine — stays supported:

```bash
dotnet publish src/FleetHubAgent/FleetHubAgent.csproj -c Release -o dist
scp dist/fleethub-agent user@target:/tmp/
ssh user@target 'curl -fsSL .../install.sh | sudo bash -s -- --binary /tmp/fleethub-agent --secret "..."'
```

To cut the next one — the tag prefix and the asset name are what `install.sh` matches on, so
both must be exact:

```bash
dotnet publish src/FleetHubAgent/FleetHubAgent.csproj -c Release -o dist
gh release create linux-agent-v0.2.0 dist/fleethub-agent \
  --title "Linux agent v0.2.0" --notes "..."
```

The installer reads the releases list with `per_page=100` rather than the default 30. This repo
already carries 50+ releases and Windows agent releases are frequent while Linux ones will be
rare, so the newest `linux-agent-v*` sinks down the list — past a page boundary it would be
reported as "no published release" for a release that plainly exists.

Then sign the manifest so deployed agents pick it up. `sign_release.py` already takes a
`--manifest` path, so it signs this one with no change to it — same offline key as the Windows
agent:

```bash
python sign_release.py --sign-agent   --file agent-linux/dist/fleethub-agent   --manifest agent-linux/agent.manifest.json   --agent-version 0.2.0   --agent-url https://github.com/aw08-2004/Temp_Monitor/releases/download/linux-agent-v0.2.0/fleethub-agent
```

Commit `agent-linux/agent.manifest.json` **and** its `.sig` to `main` — the agent reads them
from the branch. `.gitattributes` pins both with `-text`; never let a tool rewrite their line
endings, because a signature covers exact bytes and a rewritten file is indistinguishable from
a tampered one. The fleet would then refuse every update with nothing in any log but a debug
line.

Note the ordering that follows from all this: **upload the release asset before committing the
manifest.** The manifest names a URL, and an agent that reads it in between gets a verified
manifest pointing at a 404.

## Running as root

It does, and the unit file says why in full. Briefly: `/sys/class/dmi/id/product_serial` is
`0400`, and that serial is how the hub collapses duplicate machines — an unprivileged agent
renames a machine into a permanent duplicate. `restart`, `shutdown` and `run_script` are root
operations by definition. A capabilities-only user would be root with extra steps and a false
sense of containment; the containment that matters is at the hub, where `ALLOWED_EMAILS` plus
permission groups decide who may send a command at all.
