# FleetHub

CPU temperature monitoring and remote fleet management across machines. Each
machine runs an agent that reads sensors and reports them to a central **hub**
(Flask + Socket.IO) for live charts, history, and remote commands.

- Hub: `app.py` (served via `wsgi.py`), live at https://your.domain.com
- **Agent (recommended, new machines):** `agent/` — a C#/.NET Windows Service.
  See [agent/README.md](agent/README.md).
- **Companion (legacy, existing installs):** `companion.py` — a Python scheduled-task
  agent. Self-updates now migrate it to the C# agent automatically (see below).
- Unified installer (Agent / Companion / Hub): `install.ps1`

## Two agents, one hub

The hub accepts telemetry and fleet commands from either agent — they speak the
same wire protocol (`POST /api/report` for telemetry; `/api/agent/*` for the fleet
command channel). You don't need to run both on the same machine; the migration
path below moves each machine from one to the other automatically.

|                              | Companion (`companion.py`)         | Agent (`agent/`)                          |
|------------------------------|-------------------------------------|--------------------------------------------|
| Language / runtime           | Python, needs a Python install      | C#/.NET, self-contained single-file exe    |
| Runs as                      | Per-logon Scheduled Task (user session) | Windows Service (**SYSTEM**, session 0) |
| Sensors                      | LibreHardwareMonitor.exe + its `:8085` web server | LibreHardwareMonitorLib, in-process |
| Telemetry (`/api/report`)    | Yes                                  | Yes (parity)                                |
| Fleet commands (restart/rename/scripts/etc.) | No                    | Yes — enroll/heartbeat/poll/execute/report |
| Self-update                  | Signed Python source swap           | Signed binary swap (manifest + sha256)      |
| Status                       | Legacy — auto-migrates away          | Recommended for all new installs            |

## Installing the agent (recommended, new machines)

See [agent/README.md](agent/README.md) for the full C#/.NET agent: build/publish,
signing/release process, and `agent/install/agent-install.ps1` (installs the Windows
Service, the PawnIO sensor driver, and SCM failure-recovery for self-updates).

## Unified installer

`install.ps1` at the repo root is a single menu-driven installer covering all
three pieces -- Agent, Companion, and Hub. Run it with no arguments for an
interactive menu; it prompts for whatever the chosen path needs (enrollment
secret, hub URL, OAuth creds, ...), defaulting to values already in a local
`.env` when run from a clone. The installer elevates itself automatically if
not already run as admin.

**From the web:**

```powershell
irm https://raw.githubusercontent.com/aw08-2004/Temp_Monitor/main/install.ps1 | iex
```

**Non-interactive**, pass `-Component` plus the relevant parameters (`iex`
alone can't take arguments, so invoke the fetched script as a scriptblock
instead):

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/aw08-2004/Temp_Monitor/main/install.ps1))) -Component Agent -AgentUrl <url> -EnrollmentSecret <secret>
```

**From a local clone:**

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1                                   # interactive menu
powershell -ExecutionPolicy Bypass -File install.ps1 -Component Agent
powershell -ExecutionPolicy Bypass -File install.ps1 -Component Companion
powershell -ExecutionPolicy Bypass -File install.ps1 -Component Hub
powershell -ExecutionPolicy Bypass -File install.ps1 -Component Agent -Uninstall
powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall                        # bare -Uninstall = legacy companion, for back-compat
```

Component-specific parameters: `-InstallDir <path>` / `-Port <port>` (Companion,
defaults `C:\Program Files\TempMonitor` / `8085`); `-AgentUrl` / `-AgentExe` /
`-EnrollmentSecret` / `-HubUrl` (Agent); `-HubPort <port>` (default `3001`) and
`-HubInstallDir <path>` (default `C:\Program Files\FleetHub\Hub`) (Hub).

Hub and Agent install side by side under one root — `C:\Program Files\FleetHub\Hub`
and `C:\Program Files\FleetHub\Agent`. The Hub installs as the **`FleetHub - Hub`
Windows Service** (Python wrapped with WinSW, running as LocalSystem).

The Hub install downloads only the files the hub actually needs (~0.3 MB: the Python
modules, `templates/`, `static/`, `requirements.txt`) rather than cloning the whole
repo, so the agent tree, tests and docs never land on a server. `git` is no longer
required on the hub box.

Installs made before the FleetHub rename are detected and migrated: the old service is
removed, `.env` and `logs/` (including the telemetry DB) are moved to the new root, and
an existing agent's binary is moved with its service re-pointed at the new path.

## Installing the companion agent (legacy)

Existing installs keep working as-is; new installs should use the agent above
instead. Run on the Windows machine you want to monitor. The installer needs an
elevated (admin) PowerShell, since LibreHardwareMonitor needs admin rights to
read sensors and the agent runs via scheduled tasks.

### What it installs

- Python 3 (via `winget`, if missing) + the `requests`/`cryptography` packages
- LibreHardwareMonitor (latest GitHub release), configured to run its web
  server on the configured port
- PawnIO (skipped if the `PawnIO` service already exists) -- the kernel
  driver LibreHardwareMonitor needs to read sensors on modern Windows
- `companion.py`, pulled from `main`
- Two scheduled tasks (run at logon, admin rights): LibreHardwareMonitor,
  then the companion agent 30s later

### Uninstalling

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall
```

Removes the scheduled tasks, stops the running processes, and deletes the
install directory. Python itself is left alone.

## Self-updates

`companion.py` checks `VERSION` against the copy on `main` every start, and
weekly thereafter while running, swapping itself for the newer version when
found. No separate version file is needed — bump the `VERSION` constant near
the top of `companion.py` on every push to `main`, or nothing will update.
From **2.8.0** onward, updates are Ed25519-signed (see [Signing releases](#signing-releases)
below) — an unsigned or tampered `companion.py` is refused, not applied.

### Hub self-updates (opt-in)

The hub can keep itself current too, but it's **off by default** — set
`HUB_AUTO_UPDATE=1` in the hub's `.env` to enable it (a dev clone left unset never
touches itself). When enabled, the hub checks `HUB_VERSION` on `main` every 15
minutes; when `main` is ahead it updates itself, best-effort re-installs
`requirements.txt`, then exits non-zero so the `FleetHub - Hub` Windows Service
auto-restarts waitress on the new code (WinSW `onfailure`, ~5 s downtime).

How it updates depends on the layout, decided by whether a `.git` directory is present:

- **Files-only install** (what the installer now produces): downloads the branch
  archive and replaces the hub runtime file set. The whole archive is staged and
  checked for completeness first, so a truncated download leaves the hub untouched
  rather than half-updated. `.env`, `logs/` and the service wrapper are never written.
- **Git clone** (dev checkouts, and hubs deployed before the change): `git fetch` +
  `git reset --hard origin/main`, mirroring `main` exactly — **local changes on the
  hub box are discarded**. Requires `git` on `PATH`.

Unlike the companion/agent trains, neither path uses the Ed25519 release key: both
trust GitHub over HTTPS plus push access to `main` (the pinned git origin for a clone,
the branch archive over TLS for a files-only install). The Ed25519 trust root still
gates agent binaries and is untouched by this. As with every hub change, bump
`HUB_VERSION` near the top of `app.py` on each push to `main`, or the hub won't know
to update. (The installer offers to set
`HUB_AUTO_UPDATE=1` for you; on hubs still on the older scheduled-task deployment the
same exit instead relies on the task's 2-minute repetition.)

### Migration to the C# agent (automatic, from companion 2.10.0)

From companion **2.10.0**, every machine that self-updates checks whether the
C#/.NET agent (`TempMonitorAgent` Windows Service) is already installed. If not, it:

1. Fetches the agent's signed release manifest (`agent/agent.manifest.json` + `.sig`,
   same Ed25519 trust root as companion's own self-update) and verifies it — fail
   closed, same as every other signed artifact in this repo.
2. Downloads and runs `agent/install/agent-install.ps1` (companion already runs
   elevated, so no extra UAC prompt) to install the Windows Service. No enrollment
   secret is passed — the new agent runs telemetry-only until an operator enrolls it
   separately, exactly like a fresh manual install.
3. Confirms the service reaches `RUNNING` before trusting it.
4. Once confirmed, **decommissions companion.py**: unregisters its own scheduled
   tasks, stops LibreHardwareMonitor (the agent reads sensors in-process and doesn't
   need it), and exits. This is a clean handoff, not a dual-run period — both agents
   report the same machine name, so running both at once would double every reading.

This is fully automatic and fleet-wide: it is **not** gated behind a flag. It's
designed to fail safe at every step — a failed install (network issue, blocked
driver install, etc.) just leaves companion.py running normally and retries (capped
at 5 attempts, once a day) on the next check; nothing removes the fallback until the
new agent is confirmed running. Set `MIGRATION_ENABLED = False` near the top of
`companion.py` and push as a hotfix if this ever needs to be paused fleet-wide.

## Hub

`app.py` receives reports at `POST /api/report` (open, no auth -- agents
must be able to post without signing in), and serves these views (gated
behind Google sign-in, see below):

- `/` -- a card per machine (live temp, status, uptime); click one to open
  its detail page
- `/machine/<name>` -- that machine's live temp, uptime, agent version,
  asset tag/serial number/model, and its own history chart (day picker +
  live updates for today)
- `/history` -- daily summary/average across all machines

Data is persisted to `logs/temp_v2.db` (SQLite) with optional CSV archiving;
rotated log files also live under `logs/`. Run it via `wsgi.py`, or directly
with:

```powershell
python app.py
```

### Google sign-in setup

Viewing the dashboard (`/`, `/machine/<name>`, `/history`, and the
`/api/history`, `/api/daily_summary`, `/api/machines`, `/api/machines/<name>`
endpoints, plus live Socket.IO updates) requires signing in with an
allow-listed Google account. `POST /api/report` is intentionally exempt so
agents never need credentials.

1. In the [Google Cloud Console](https://console.cloud.google.com/apis/credentials),
   create an **OAuth 2.0 Client ID** (Application type: Web application).
2. Add an authorized redirect URI: `https://your.domain.com/auth/callback`
   (and `http://localhost:3001/auth/callback` for local dev).
3. Set the following as environment variables, or in a `.env` file next to
   `app.py` (gitignored):

   ```
   GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=your-client-secret
   FLASK_SECRET_KEY=a-long-random-string   # signs the session cookie
   ALLOWED_EMAILS=you@example.com,teammate@example.com
   HUB_URL=https://your.domain.com         # public URL of this hub
   ```

`app.py` fails fast at startup if any of these are missing. Only emails in
`ALLOWED_EMAILS` (comma-separated, case-insensitive) can complete sign-in;
everyone else gets a 403 after authenticating with Google.

## Fleet command channel (RMM)

Beyond telemetry, the hub can queue **commands** for a machine that its agent
pulls and executes (restart, rename, install, etc.). This is the hub→agent
direction, added by [fleet.py](fleet.py) (core logic) and
[fleet_web.py](fleet_web.py) (HTTP surface), with state in the same SQLite DB
(`agents`, `commands`, `command_results`, `audit_log`). **The C#/.NET agent
(`agent/`) implements the client side of this channel; `companion.py` does not**
(it's telemetry-only, which is why it migrates itself away — see above).

**Security model.**

- **Agent enrollment**: an agent presents a shared `AGENT_ENROLLMENT_SECRET` to
  `POST /api/agent/enroll` and receives a per-agent bearer token (only its hash
  is stored). All other agent endpoints require `Authorization: Bearer
  <agent_id>:<token>`. With the secret unset, no agent can enroll (fail closed).
- **Issuing a command requires nothing but a signed-in, allow-listed session.**
  Every type — including `run_script`, which runs arbitrary PowerShell **as SYSTEM** —
  dispatches on that alone. So:

  > ⚠️ **`ALLOWED_EMAILS` is the entire perimeter for remote code execution as SYSTEM
  > across the fleet.** Anyone on that list can run anything, anywhere, at any time.
  > Treat adding an address to it as granting domain-admin-equivalent power, and keep
  > those accounts on MFA.

  This is deliberate: the channel is operated by a helpdesk group, and the previous
  design (below) could not serve more than one person.
- **The `audit_log` is the accountability control.** Every enroll / issue / claim /
  complete is appended, and `issue_command` records the issuing operator's email plus
  the **full command params, including script text**. With no second gate, that trail
  is the only answer to "who ran this?" — so it must never be allowed to go quiet.
- **CSRF**: the console endpoints only accept `application/json` bodies. That is
  load-bearing, not incidental — it's what stops a cross-site POST from a signed-in
  operator's browser becoming fleet-wide RCE (that content type isn't CORS-safelisted,
  so cross-origin requests preflight and fail; an HTML form can't produce it). The
  session cookie is additionally pinned `SameSite=Lax` + `Secure`. Don't add
  `force=True` to a `get_json()` call, a form-encoded fallback, or permissive CORS.

<details>
<summary>Previously: signed high-risk commands (removed in hub 1.10 / agent 3.1)</summary>

`run_script`, `install_driver` and `update_bios` used to additionally require an
**offline Ed25519 signature** over the canonical payload, verified by the hub and
re-verified by the agent, produced with `sign_release.py --sign-command`. It assumed a
single operator holding the private key and gave a helpdesk group no way to run a script
without that person signing it for them.

It was also never actually live: no `COMMAND_SIGNING_PUBLIC_KEY_HEX` was ever configured
on the hub, and `AgentConfig`'s embedded key was left empty, so both ends failed closed
and **every high-risk command was refused outright**. Removing the gate is what made
`run_script` work at all; it did not loosen a working control.

Self-update signing is a **separate trust root and is untouched** — see
[Signing releases](#signing-releases).
</details>

Add to the hub's environment / `.env`:

```
AGENT_ENROLLMENT_SECRET=a-long-random-shared-secret
```

**Endpoints.** Agent-facing (token auth): `POST /api/agent/enroll`,
`POST /api/agent/heartbeat`, `GET /api/agent/commands` (pull + claim),
`POST /api/agent/commands/<id>/result`. Console-facing (Google sign-in):
`GET /api/fleet/status` (online/offline), `GET|POST /api/fleet/commands`,
`GET /api/fleet/commands/<id>`. Every issue/claim/complete/enroll is written to
`audit_log`.

## Package deployment (PDQ-style)

Define an installer once — payload, silent command line, what proves it worked — then
aim it at machines and watch it land. Core logic in [packages.py](packages.py), HTTP
surface in [packages_web.py](packages_web.py), UI at `/packages`, agent side in
`agent/src/TempMonitorAgent/Fleet/Executors/DeployPackageExecutor.cs`. State lives in the
same SQLite DB (`packages`, `package_sources`, `deployments`, `deployment_targets`).

**A package is a recipe plus a payload.** The payload is either a file uploaded to the
hub (stored beside the database under `logs/packages/`, content-addressed by SHA-256 and
shared between packages built from the same installer) or an external reference — a
winget id, an `https://` URL, or a UNC path. The recipe is the command line (with
`{file}` standing in for the resolved payload), a timeout, the accepted exit codes, and a
detection rule.

**Success is exit code AND detection, both.** An installer exiting 0 is evidence, not
proof — silent installers routinely return 0 having done nothing, and on a fleet-wide push
that is the failure you least want reported as success. So every package also carries a
post-install check: a file exists, a registry value exists (optionally matching exactly),
or a product appears in Windows' installed-programs list at or above a given version.
Anything the agent cannot evaluate counts as *not* detected, never as detected.

**Trust.** The hub computes the SHA-256 of an uploaded payload itself, at upload, from the
bytes it writes — a client-supplied digest is never accepted — and the agent re-verifies
it before executing, deleting the file on a mismatch. That plus the authenticated HTTPS
channel is the whole integrity story; there is deliberately no new offline signing key
(see the command-channel section above for why that model was removed). URL/UNC payloads
can be hash-pinned too; winget has its own trust chain.

**Scheduling layers on the existing command queue, it does not replace it.** A deployment
holds one row per target machine; the hub's scheduler thread turns a due target into an
ordinary `deploy_package` command with the usual TTL, then reads that command's terminal
status back. A machine that is offline therefore costs one expired command and one
backoff, using the same expiry the queue already enforces. Retries are per machine
(default 3 attempts, backoff doubling from 15 minutes), and a deployment can carry a
window — don't start before X, give up after Y.

**Authorization** is the `deploy_packages` capability plus machine scope, from Permission
Groups. Targets are checked *before* anything is written and the request is refused whole
if any single machine is out of scope — a deploy that quietly installs on nine of the ten
machines you asked for is worse than one that fails. Reads are scoped the other way: an
operator sees only the target rows they could have created.

**Endpoints.** Console (`deploy_packages`): `GET|POST /api/packages`,
`GET|PUT|DELETE /api/packages/<id>`, `POST /api/packages/upload`,
`GET|POST /api/deployments`, `GET /api/deployments/<id>`,
`POST /api/deployments/<id>/cancel`, `POST /api/deployments/<id>/retry`.
Agent (token auth): `GET /api/agent/packages/<sha256>`.

> The upload endpoint is the one place that accepts `multipart/form-data` rather than
> JSON — a file upload cannot be JSON. It is deliberately inert to compensate: it stores
> bytes and returns a hash, creating no package and touching no machine. Turning that hash
> into something that runs anywhere requires the JSON-bodied endpoints, which a cross-site
> form cannot reach. Don't make it create a package as a convenience.

Tunables live in Settings under **Package Deployment**: retry defaults, the upload size
limit, and the scheduler interval.

## Signing releases

Two artifacts in this repo are Ed25519-signed so a compromised hub or repo commit
can't push code that runs as admin fleet-wide unverified:

- **`companion.py`** — re-sign with `python sign_release.py` after every edit, then
  commit `companion.py` + `companion.py.sig` together. `.gitattributes` pins both
  `-text` so git never rewrites line endings (the signed bytes must match exactly
  what clients download).
- **The C# agent** (`agent/`) — see [agent/README.md](agent/README.md) for
  `agent/release.ps1` (automates the whole release: version bump, publish,
  GitHub release, sign, upload) or the manual `sign_release.py --sign-agent` steps.

One-time setup: `python sign_release.py --genkey`, keep the private key OFF the
repo, paste the printed public key into `companion.py`'s `UPDATE_PUBLIC_KEY_HEX`
(the C# agent's `AgentConfig.UpdatePublicKeyHex` reuses the same key/trust root).

This is the **release** trust root: it governs what *code* the fleet is allowed to run,
and it is fully enforced. It is unrelated to fleet *commands*, which are no longer signed
(see [Fleet command channel](#fleet-command-channel)). Don't conflate the two — a
compromised hub still must not be able to push a malicious binary, which is exactly what
these signatures prevent.
