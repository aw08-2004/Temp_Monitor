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
  asset tag/serial number/model, a Storage card with one % occupied tile per
  volume (used/total/free), and its own history charts: CPU, memory, disk usage,
  disk read/write, network in/out, GPU, temperature (day picker + live updates
  for today). The throughput panels auto-scale their units, so an idle NIC reads
  in KB/s and a busy NVMe in MB/s on the same axis format.
- `/history` -- daily summary/average across all machines
- `/alerts` -- conditions that want attention: machines running hot, and
  duplicate machines that share a serial while both online (see below)

### Temperature alerts

A machine is flagged as overheating when its **average** temperature over a
window (default **5 minutes**, `hub.overheat_avg_window_seconds` in Settings) is
at or above the overheat threshold (`hub.overheat_threshold`). Averaging is the
point: a momentary spike no longer raises an alert, only a sustained condition
does.

The check runs on the hub every ~30 s (`evaluate_overheat_once`), so alerts are
independent of any browser being open. An overheat alert appears in the **Alerts
tab**, not on the Dashboard -- the Dashboard is a live temp/status view. It
**auto-resolves** once the average drops back below the threshold (or the machine
goes offline), and can be dismissed manually. Alerts are machine-scoped: an
operator only sees, and is only badge-counted for, machines within their scope.

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

## Backups

A consistent snapshot of the hub database, compressed, encrypted **on the hub** and pushed
offsite on a schedule. Core logic in [backups.py](backups.py), HTTP surface in
[backups_web.py](backups_web.py), UI at `/backups`, and the restore tool at
[restore_backup.py](restore_backup.py). State lives in the same SQLite DB
(`backup_destinations`, `backup_runs`, `backup_state`, plus `backup_machine_config`,
`backup_file_sets`, `backup_files` and `backup_restores` for the per-PC half below).

**`VACUUM INTO`, never a file copy.** The database is opened WAL and written live by the
ingest path and the `db_writer` thread. Copying `temp_v2.db` while that is happening gives
you a torn file plus a `-wal` sidecar you didn't copy — a backup that restores to
"database disk image is malformed", discovered on the day you need it. `VACUUM INTO` asks
SQLite for a transactionally consistent, already-compacted snapshot instead.

**The provider only ever sees ciphertext.** Snapshot → gzip → AES-256-GCM in 4 MiB chunks
→ HTTPS PUT. Each artifact gets its own random data key, wrapped by the master key. Chunk
AAD binds `sha256(header) ‖ counter ‖ final-flag`, so a tampered header, a reordered chunk
and — the one that actually happens — a **truncated upload** all fail to decrypt rather
than restoring as a plausible-looking corrupt database.

> ### ⚠️ The master key is not recoverable
>
> `BACKUP_MASTER_KEY` in `.env` is the only thing that can decrypt your backups. It is
> deliberately **not** in the hub database — a key stored inside the thing it protects
> protects nothing. If this server is lost and the key was never written down elsewhere,
> every backup ever taken is permanently unreadable.
>
> The hub generates it once, shows it once, and nags on the Backups page until an operator
> confirms it is stored somewhere else. Every reveal is written to the audit log.
>
> To restore, you need the key and the file — **nothing else**. No hub, no database, no
> network:
>
> ```
> python restore_backup.py --in 20260721T030000Z-temp_v2.db.gz.fhb --out temp_v2.db --verify
> python restore_backup.py --in <file>.fhb --info     # just read the header
> ```
>
> Then stop the hub service, move the old `logs/temp_v2.db` aside (with its `-wal`/`-shm`),
> drop the restored file in, and start the service. `--verify` runs
> `PRAGMA integrity_check` first, which is worth the seconds.

**Destinations: S3-compatible or WebDAV, your choice per destination.** S3 covers AWS,
MinIO, Backblaze B2 and Wasabi — signed with SigV4 implemented in ~100 lines of stdlib
`hmac` rather than pulling ~80 MB of botocore onto a hub whose sparse install is 0.3 MB,
and checked against AWS's published test vectors in the suite. WebDAV covers Nextcloud,
ownCloud and IIS, with Basic auth over TLS. Plain `http://` is refused for anything but a
loopback host, so a typo cannot ship your credentials in clear.

**Credentials never touch the `settings` table.** Settings get rendered into a form,
returned wholesale by `as_dict()`, and partly shipped to agents in `agent_config()` — an
S3 secret key belongs in none of those. They live encrypted with the master key in
`logs/backup_secrets.json`, addressed by destination id (which is the AAD, so a credential
blob copied between destinations fails rather than authenticating somewhere unintended).
They go in and are never returned — the edit form's empty credential field means
"unchanged".

**Rotation reads the bucket, not a local record.** After each successful upload, artifacts
beyond `backup.hub_keep_generations` are deleted from the destination. Ordering comes from
the object key, which is timestamp-prefixed, so "newest N" is a lexicographic sort with no
dependence on remote mtime (S3 and WebDAV report it differently, from different clocks). A
generation you delete by hand stays deleted; `keep < 1` is refused rather than emptying the
bucket.

**Authorization** is the `manage_backups` capability — and it is deliberately *not*
machine-scoped. A hub database backup is the whole hub, so there is no coherent way to
hand it to an operator who sees nine machines out of forty. Read `manage_backups` as "can
eventually read everything, via a restore". The same capability also writes the four
`backup.*` schedule settings through `PUT /api/backups/schedule`; without that, arming a
backup would need `manage_settings` too, which would defeat the point of a narrow
capability.

**Endpoints** (all `manage_backups`): `GET /api/backups`, `GET /api/backups/runs`,
`POST /api/backups/key`, `POST /api/backups/key/reveal`,
`POST /api/backups/key/escrowed`, `POST /api/backups/destinations`,
`PUT|DELETE /api/backups/destinations/<id>`, `POST /api/backups/destinations/<id>/test`,
`PUT /api/backups/schedule`, `POST /api/backups/run`.

The per-machine routes need `manage_backups` **and** that machine in scope:
`GET|PUT /api/backups/machines/<machine>`, `GET /api/backups/machines/<machine>/manifest`,
`POST /api/backups/machines/<machine>/restore`,
`GET /api/backups/machines/<machine>/restores`.

> Revealing the key is a **POST with a JSON body**, not a GET — so it cannot be triggered
> by a link, an `<img src>`, or anything else a browser fetches on someone's behalf. Keep
> it that way.

Tunables live in Settings under **Backups**, or on the Backups page itself: on/off,
destination, interval, and generations to keep.

### Per-PC file backups

Configured on the Backups page's **Backup Settings** tab, per machine on that machine's
**Backup** tab. Path selection lives in [backup_paths.py](backup_paths.py).

**You never enumerate user profiles.** A pattern is written once with tokens and expanded
on each PC at backup time, so it keeps being right as people come and go:

| Token | Expands to |
|---|---|
| `%Users%` or `%User%` | every real profile (skips Public, Default, service accounts) |
| `%Desktop%` `%Documents%` `%Downloads%` `%Pictures%` `%Favorites%` `%AppData%` `%LocalAppData%` | that user's **actual** folder, per user |
| `%ProgramData%` `%SystemDrive%` `%windir%` `%ProgramFiles%` | machine-wide, no fan-out |

```
%Desktop%                      →  C:\Users\bob\OneDrive - Contoso\Desktop
                                  C:\Users\carol\Desktop
%User%\Scripts                 →  C:\Users\bob\Scripts, C:\Users\carol\Scripts
C:\Users\%Users%\Projects      →  C:\Users\bob\Projects, C:\Users\carol\Projects
```

`%User%` and `%Users%` are the same token — use whichever reads better. `%Users%` suits a
pattern on its own; `%User%\Scripts` suits a custom subfolder. Note that `%User%` does
**not** mean "whoever is logged in now": backups run as SYSTEM on a schedule, routinely
with nobody signed in, so every per-user token covers every profile.

> **Use `%Desktop%`, not `C:\Users\%Users%\Desktop`, for the standard folders.** With
> OneDrive Known Folder Move — common in orgs — the literal path is an empty stub and the
> real data lives under the OneDrive folder. The token reads each user's shell-folder
> registry and follows the redirection; the literal path does not, and would back up
> nothing while reporting success every night.

**An unknown token is refused, never treated as a literal.** `%Userss%` would otherwise
match nothing forever, with a green run beside it. Excludes take the same tokens plus
globs (`*.tmp`, `**\node_modules\**`); a pattern with no backslash matches on filename
anywhere, and excluding a folder excludes everything in it.

The **Preview** panel resolves your patterns against a real machine's reported profiles,
so you can see the actual folders — and the problems ("carol has no %Documents% folder
recorded") — before anything runs.

**Incremental, in chains.** Each run uploads only what changed; a full is forced every
`backup.files_full_every` runs. Rotation deletes **whole chains** — never an archive
inside one, because an incremental without its full restores to nothing.

**A PC that was switched off is not skipped — it catches up.** The scheduler only
dispatches to machines it can currently reach, so a laptop that was closed at 03:00 stays
*due* rather than being marked as attempted, and backs up within a minute of coming back
online. (Queuing a backup for an unreachable machine used to move its clock forward, so
it missed that night *and* the next one.)

**Back up now**, on the machine's **Backup** tab for one PC, or on **Backup Settings** for
everything in your scope. This also works on a machine that is switched off: the request
is remembered and answered when the PC reappears, so the button reports *started* or
*queued* rather than failing. Pressing it twice does not queue two backups, and a machine
already backing up will not start a second run — the request waits its turn.

**Cancel**, in the same two places. What it can stop depends on how far the backup got:
a *queued* request is dropped; a backup that has been sent to the PC but not yet started
is stopped before it begins; and one the PC has **already started** is marked cancelled —
it stops holding a concurrency slot and its result is discarded, but the PC finishes the
transfer it is in the middle of, because there is no way to recall a job an agent has
already picked up. The response says which of these happened. Any archive a cancelled
backup manages to upload is deleted automatically, so a cancel never leaves junk in the
bucket.

> A fleet-wide "back up now" can bring a lot of machines back at once, so
> `backup.files_max_concurrent` (default **3**) limits how many run simultaneously.
> The rest start automatically as slots free up; set it to 0 to remove the limit.

**Agents never hold the destination credential.** For S3 the hub mints a pre-signed PUT
scoped to that machine's folder; for WebDAV the agent uploads to the hub, which streams it
onward. And each machine gets a **derived** key, `HKDF(master, machine)`, not the master —
so a stolen laptop's key opens that laptop's backups and nothing else. Restore is still
one argument: the envelope header names the machine, and the master re-derives.

**Open files are captured via VSS.** The agent creates a Volume Shadow Copy
(`Win32_ShadowCopy`, the client-SKU route — `vssadmin create shadow` is Server-only) and
reads from the snapshot, so an Outlook PST or a document someone left open is still
backed up. If a snapshot cannot be created the run continues against the live filesystem
and reports which files it could not read, rather than failing.

**Junctions are never followed.** A Windows profile contains junctions pointing at their
own ancestors; following them is an infinite walk.

### Restoring a PC's files

The **Backup** tab on a machine's page browses what that machine has backed up — a folder
at a time, with a search box for when you know the filename but not the path. What it
lists is what is actually **recoverable**: files the user has since deleted, and chains
that have rotated away, are already gone from the answer, so you are never offered a
restore that fails halfway.

Tick files or folders, then choose:

- **Restore onto** — this machine, or another one. Restoring PC-3's data onto a brand-new
  PC-9 is the hardware-replacement case, and it needs you to have access to *both*
  machines: reading one PC's files and writing files onto another are separately checked.
- **Write to** — a folder like `C:\Restored` (files land under it in their original tree,
  and nothing live is touched), or blank to put them back where they came from.
- **Overwrite** — off by default, so a restore alongside surviving files is the safe path.

A ticked **folder** restores everything that was ever under it, including files that are
no longer in the folder you are looking at. The hub works out which archives hold which
version of each file and hands the agent a scoped, short-lived download per archive — the
same brokering as the upload path, so no machine ever holds the destination credential.

A restore that writes fewer files than you asked for is reported as **failed**, with the
counts and the first few reasons. "Restored 900 of 1000" needs someone to look at the
other 100, and a green row means nobody does.

> **Recovering without the hub.** The archive is a tar inside the same encrypted envelope,
> so the standalone tool opens it with the master key alone — useful when the hub is gone,
> or when you want one file without pushing anything onto a PC:
>
> ```bash
> python restore_backup.py --in 20260721T030000Z-a1b2c3-000-full.fhb --list
> python restore_backup.py --in <file>.fhb --extract C:\Recovered
> python restore_backup.py --in <file>.fhb --extract C:\Recovered --match "*/Desktop/*"
> ```
>
> One master key opens every machine: the per-machine key is derived from it, and the
> envelope header says which machine to derive for.

> **Status:** built end to end — hub 1.30.0 and agent 3.9.0. The agent needs a signed
> release (`sign_release.py --sign-agent`) before the fleet gains any of this, and the hub
> should be deployed first. See [features.md](features.md).

## Remote view & control

Live remote view **and control** of a managed PC over WebRTC (H.264), from the machine
page's **Remote** tab. Gated on the `remote_control` capability plus the machine being in
the operator's scope; every session start/stop is in the audit log.

- **How it works.** The agent runs as SYSTEM in session 0, which has no desktop to capture,
  so on session start it injects a helper (the same signed agent binary, `--remote-helper`)
  as SYSTEM into the interactive session. The helper captures the screen (DXGI), encodes
  H.264, and streams it to the operator's browser over WebRTC; the browser sends mouse and
  keyboard back over a data channel (`SendInput`). Ctrl+Alt+Del is supported.
- **Consent** is `unattended` by default (connects immediately, standard RMM) or `attended`
  (the logged-in user must approve first). Set it in **Settings → Remote Control**.
- **TURN.** Agents sit behind arbitrary NATs, so WebRTC media usually needs a TURN relay.
  **The hub is the TURN server**: run [coturn from `turn/`](turn/README.md) on the hub host
  and set a shared secret in the hub's `.env`:

  ```
  REMOTE_TURN_SECRET=a-long-random-shared-secret
  ```

  The hub mints short-lived per-session TURN credentials from it (nothing to manage
  per-user). Then set **Settings → Remote Control → TURN servers** to `turn:<hub-host>:3478`.
  Leaving `REMOTE_TURN_SECRET` unset simply omits TURN (STUN/direct paths only — fine on a
  LAN). See [turn/README.md](turn/README.md) for ports, the public-IP requirement, and the
  Windows-host notes.

> **Status:** built — hub 1.39.0. The agent half needs a signed release
> (`sign_release.py --sign-agent`), and the hub should be deployed first. On-hardware
> follow-ups (tune together): secure-desktop capture during UAC, hardware H.264 encode, and
> per-machine consent override.

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
