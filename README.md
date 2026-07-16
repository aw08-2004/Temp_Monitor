# Temp Monitor

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
`-EnrollmentSecret` / `-HubUrl` / `-CommandSigningPublicKey` (Agent); `-HubPort
<port>` (Hub, default `3001`) -- Hub setup must be run from a local clone since
it needs the full app, not just one file.

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

**Security model.** The channel is built to be safe even against a compromised
hub:

- **Agent enrollment**: an agent presents a shared `AGENT_ENROLLMENT_SECRET` to
  `POST /api/agent/enroll` and receives a per-agent bearer token (only its hash
  is stored). All other agent endpoints require `Authorization: Bearer
  <agent_id>:<token>`.
- **Command tiers**: low-risk commands (`restart`, `shutdown`, `rename`,
  `gpupdate`, `install_app`) dispatch on an authorized console session alone.
  High-risk commands (`run_script`, `install_driver`, `update_bios`) additionally
  require an **offline Ed25519 signature** over the canonical payload — verified
  by the hub *and* re-verified by the agent before executing, using the agent's own
  classification of what counts as high-risk (so a compromised hub can't downgrade
  a command just by clearing the flag). The private key lives off the repo (same
  trust root as signed self-updates); sign a command with:

  ```powershell
  python sign_release.py --sign-command --type run_script --machine PC-01 --params '{"script":"..."}'
  ```

- **Fail closed**: with `AGENT_ENROLLMENT_SECRET` unset no agent can enroll; with
  `COMMAND_SIGNING_PUBLIC_KEY_HEX` unset every high-risk command is refused. Both
  are optional env vars, so telemetry-only deployments are unaffected. **Neither is
  currently configured on this hub** — set both before issuing any fleet commands.

Add to the hub's environment / `.env`:

```
AGENT_ENROLLMENT_SECRET=a-long-random-shared-secret
COMMAND_SIGNING_PUBLIC_KEY_HEX=<64-hex Ed25519 public key from sign_release.py --genkey>
```

**Endpoints.** Agent-facing (token auth): `POST /api/agent/enroll`,
`POST /api/agent/heartbeat`, `GET /api/agent/commands` (pull + claim),
`POST /api/agent/commands/<id>/result`. Console-facing (Google sign-in):
`GET /api/fleet/status` (online/offline), `GET|POST /api/fleet/commands`,
`GET /api/fleet/commands/<id>`. Every issue/claim/complete/enroll is written to
`audit_log`.

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
`sign_release.py --sign-command` uses a separate, independently-configurable trust
root (`COMMAND_SIGNING_PUBLIC_KEY_HEX`) for the fleet command channel — see above.
