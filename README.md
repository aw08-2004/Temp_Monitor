# FleetHub

CPU temperature monitoring and remote fleet management across machines. Each
machine runs an agent that reads sensors and reports them to a central **hub**
(Flask + Socket.IO) for live charts, history, and remote commands.

- Hub: `hub/app.py` (served via `hub/wsgi.py`), live at https://your.domain.com
- **Agent:** `agent/` — a C#/.NET Windows Service. See [agent/README.md](agent/README.md).
- Unified installer (Agent / Hub / TURN relay): `install.ps1`

## The legacy Python companion is gone

Machines used to run `companion.py`, a Python scheduled task that did telemetry only.
It has been **removed from the repo**. The C#/.NET agent replaced it: it runs as a
Windows Service under SYSTEM (so it works with nobody logged on), reads sensors
in-process instead of shelling out to LibreHardwareMonitor's `:8085` web server, and
speaks the fleet command channel the companion never implemented.

Consequences of the removal, in case an unmigrated machine turns up:

- **Nothing self-updates a companion anymore.** The hub no longer advertises a 2.x
  version, and `raw.githubusercontent.com/.../companion.py` now 404s, so a surviving
  companion just keeps reporting telemetry at its pinned version forever.
- **It can no longer be installed.** `install.ps1` dropped that path.
- **It can still be uninstalled**, which is what such a machine needs — see below —
  and then given the agent.

Note the wire field is still named `companion_version` (and the agent still writes
`companion.log`). Those names are deliberately unchanged: every agent in the field
sends that key, and renaming one side alone would break exactly the machines that
are already deployed.

## Installing the agent

See [agent/README.md](agent/README.md) for the full C#/.NET agent: build/publish,
signing/release process, and `agent/install/agent-install.ps1` (installs the Windows
Service, the PawnIO sensor driver, and SCM failure-recovery for self-updates).

## Unified installer

`install.ps1` at the repo root is a single menu-driven installer covering every
component -- Agent, Hub, and the TURN relay. Run it with no arguments
for an interactive menu; it prompts for whatever the chosen path needs
(enrollment secret, hub URL, OAuth creds, ...), defaulting to values already in
a local `.env` when run from a clone. The installer elevates itself
automatically if not already run as admin.

**From the web:**

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/aw08-2004/Temp_Monitor/main/install.ps1)))
```

**Non-interactive**, pass `-Component` plus the relevant parameters (`iex`
alone can't take arguments, so invoke the fetched script as a scriptblock
instead). Anything supplied on the command line is used as given and never
prompted for again, so a fully-specified invocation runs start to finish
without input:

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/aw08-2004/Temp_Monitor/main/install.ps1))) `
    -Component Agent -HubUrl https://hub.example.com -EnrollmentSecret <secret> -AddDefenderExclusion
```

Note `-HubUrl` — the address of the hub the agent reports to. `-AgentUrl` is a
different thing: the download URL of a *specific* agent release asset, only
needed to pin a version. Left out, the installer resolves the latest release
itself. A `-AgentUrl` that isn't a `.exe`/`.zip` asset is assumed to be the hub
URL and used as such, with a warning.

**From a local clone:**

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1                                   # interactive menu
powershell -ExecutionPolicy Bypass -File install.ps1 -Component Agent
powershell -ExecutionPolicy Bypass -File install.ps1 -Component Hub
powershell -ExecutionPolicy Bypass -File install.ps1 -Component Turn                    # TURN relay only, no hub
powershell -ExecutionPolicy Bypass -File install.ps1 -Component Agent -InstallDir "D:\Apps\FleetHub\Agent"
powershell -ExecutionPolicy Bypass -File install.ps1 -Component Agent -Uninstall
powershell -ExecutionPolicy Bypass -File install.ps1 -Component Companion -Uninstall    # remove a legacy Python companion
powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall                        # bare -Uninstall = the same, for back-compat
```

`-InstallDir <path>` applies to whichever component is being installed. Left
out, each uses its own default — Agent `C:\Program Files\FleetHub\Agent`, Hub
`C:\Program Files\FleetHub\Hub`, legacy Companion `C:\Program Files\TempMonitor`.
Passed explicitly, it redirects the install *and* the Windows Defender
exclusion offered for it. Pass the same `-InstallDir` to `-Uninstall` so the
right directory is cleaned up.

Component-specific parameters: `-HubUrl` / `-EnrollmentSecret` / `-AgentUrl` / `-AgentExe` /
`-AddDefenderExclusion` / `-SkipDefenderExclusion` (Agent); `-HubPort <port>`
(default `3001`) and `-HubInstallDir <path>` (Hub — takes precedence over
`-InstallDir`); `-TurnHost` / `-TurnSecret` / `-TurnDistro` / `-TurnPort` /
`-SkipTurn` (Turn — and the same flags apply to Hub, which can set the relay up
as part of its own install).

The Defender exclusion is otherwise an interactive y/N question, defaulting to
no. `-AddDefenderExclusion` and `-SkipDefenderExclusion` pre-answer it for
unattended runs; passing both is an error.

### Installing the TURN relay on its own

`-Component Turn` provisions **only** the coturn WebRTC relay, in its own Ubuntu
WSL2 distro, with no hub. Use it when the relay belongs on a different machine from
the hub — typically one with a better public address, or when the hub runs on Linux
and you want the relay on a Windows box you already have ports forwarded to.

It asks the same questions and enforces the same preconditions as the hub's optional
TURN step (they share one code path), then prints the `turn:` / `stun:` URLs and the
shared secret to paste into **Settings → Remote Control** on the hub. If a hub *does*
happen to live on the same machine, it defaults to the secret already in that hub's
`.env`, so the two can't drift apart.

> The shared secret must match `REMOTE_TURN_SECRET` on the hub **exactly**. A mismatch
> fails every allocation with 401 and remote sessions simply never connect.

Removing it (`-Component Turn -Uninstall`) leaves the hub untouched. Uninstalling the
Hub still removes the relay too, if that machine has one.

Hub and Agent install side by side under one root — `C:\Program Files\FleetHub\Hub`
and `C:\Program Files\FleetHub\Agent`. The Hub installs as the **`FleetHub - Hub`
Windows Service** (Python wrapped with WinSW, running as LocalSystem).

The Hub install downloads the branch archive and lays down just the `hub/` subtree
(~2 MB: the Python modules, `templates/`, `static/`, `requirements.txt`) rather than
cloning the whole repo (~85 MB), so the agent tree, tests and docs never land on a
server. `git` is no longer required on the hub box.

Installs made before the FleetHub rename are detected and migrated: the old service is
removed, `.env` and `logs/` (including the telemetry DB) are moved to the new root, and
an existing agent's binary is moved with its service re-pointed at the new path.

## Removing a legacy Python companion

`companion.py` can no longer be installed, but machines that never migrated still
carry one. To clean one up:

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1 -Component Companion -Uninstall
```

This unregisters both of its scheduled tasks (`TempMonitor - Companion` and
`TempMonitor - LibreHardwareMonitor`), stops the running Python and
LibreHardwareMonitor processes, and deletes the install directory (default
`C:\Program Files\TempMonitor` — pass `-InstallDir` if it went somewhere else).
Python itself is left alone. Bare `-Uninstall` with no `-Component` does the same
thing, for back-compat with the way this was always documented.

Then install the agent on that machine as above. Do it in that order: both report
the same machine name, so running the two at once doubles every reading.

## Self-updates

The agent checks its version against the signed release manifest on `main` and swaps
its own binary for a newer one when it finds it — see [agent/README.md](agent/README.md)
for the release process. Updates are Ed25519-verified fail-closed (see
[Signing releases](#signing-releases) below): an unsigned or tampered manifest is
refused, not applied.

The hub nudges this along: `/api/report` echoes back the newest agent version it has
read from the manifest, so an agent goes and checks as soon as it sees a number ahead
of its own instead of waiting for its own weekly poll. Clients below 3.0.0 are
deliberately sent nothing at all — there is no 2.x release left to point them at, and
handing a companion a 3.x number would make it try to install an agent binary as if it
were a Python script.

### Hub self-updates (opt-in)

The hub can keep itself current too, but it's **off by default** — set
`HUB_AUTO_UPDATE=1` in the hub's `.env` to enable it (a dev clone left unset never
touches itself). The `hub.auto_update` setting overrides the `.env` value when set,
so it can also be flipped from the Settings tab. When enabled, the hub checks
`HUB_VERSION` on `main` every 15 minutes; when `main` is ahead it updates itself, best-effort re-installs
`requirements.txt`, then exits non-zero so the `FleetHub - Hub` Windows Service
auto-restarts waitress on the new code (WinSW `onfailure`, ~5 s downtime).

How it updates depends on the layout, decided by whether a `.git` directory is present:

- **Files-only install** (what the installer now produces): downloads the branch
  archive and mirrors the whole `hub/` directory over the installed one — a whole-dir
  mirror, not a hand-maintained file allowlist, because an allowlist that missed a new
  module once left the hub crash-looping. The archive is staged and checked for
  completeness first, so a truncated download leaves the hub untouched rather than
  half-updated. `.env`, `logs/` and the service wrapper live one level up in
  `STATE_ROOT`, outside the mirrored directory, so they are structurally out of reach.
- **Git clone** (dev checkouts, and hubs deployed before the change): `git fetch` +
  `git reset --hard origin/main`, mirroring `main` exactly — **local changes on the
  hub box are discarded**. Requires `git` on `PATH`.

Unlike the agent train, neither path uses the Ed25519 release key: both
trust GitHub over HTTPS plus push access to `main` (the pinned git origin for a clone,
the branch archive over TLS for a files-only install). The Ed25519 trust root still
gates agent binaries and is untouched by this. As with every hub change, bump
`HUB_VERSION` near the top of `hub/app.py` on each push to `main`, or the hub won't know
to update. (The installer offers to set
`HUB_AUTO_UPDATE=1` for you; on hubs still on the older scheduled-task deployment the
same exit instead relies on the task's 2-minute repetition.)

### Migration to the C# agent (historical)

Companion releases 2.10.0 through 2.12.0 migrated themselves: on a self-update they
verified the agent's signed manifest, ran `agent/install/agent-install.ps1`, confirmed
the service reached `RUNNING`, and only then unregistered their own scheduled tasks and
exited. That is how essentially the whole fleet moved over.

That path is gone with the script. Anything still running a companion has to be
migrated by hand — see [Removing a legacy Python companion](#removing-a-legacy-python-companion).

## Hub

`hub/app.py` receives reports at `POST /api/report` (open, no auth -- agents
must be able to post without signing in), and serves these views (gated behind
OIDC sign-in plus permission groups, see below):

- `/` -- a card per machine (live temp, status, uptime); click one to open
  its detail page
- `/machine/<name>` -- that machine's live temp, uptime, agent version,
  asset tag/serial number/model, a Storage card with one % occupied tile per
  volume (used/total/free), a Cooling card with one tile per fan (RPM plus the
  duty cycle the board is asking for, flagged when a fan is driven but not
  turning), and its own history charts: CPU, memory, disk usage, disk
  read/write, network in/out, GPU, fan speed, CPU/GPU package power, temperature
  (day picker + live updates for today). The throughput panels auto-scale their
  units, so an idle NIC reads in KB/s and a busy NVMe in MB/s on the same axis
  format. Cards and panels for hardware a machine doesn't have are hidden rather
  than left reading "--": an office PC with no discrete GPU has no GPU card, and
  a laptop that exposes no fan has no Cooling card. Below them, an **All
  sensors** section lists everything else the agent reports -- the whole
  LibreHardwareMonitor tree, grouped hardware -> category -> sensor, including
  the readings nothing charts (VRM temperatures, rail voltages, battery charge
  and wear). Collapsed by default and only polled while open.
- `/alerts` -- conditions that want attention: machines running hot, and
  duplicate machines that share a serial while both online (see below)
- `/inventory` -- one row per machine of the hardware/asset facts agents report
- `/audit` -- the audit log (see below); `/packages`, `/backups` -- their own
  sections below; `/settings`, `/users`, `/permissions` -- administration

### Temperature alerts

A machine is flagged as running hot when its **average** temperature over a
window (default **5 minutes**, `hub.high_temp_avg_window_seconds` in Settings) is
at or above the high-temperature threshold (`hub.high_temp_threshold`). Averaging
is the point: a momentary spike no longer raises an alert, only a sustained
condition does.

The check runs on the hub every ~30 s (`evaluate_high_temp_once`), so alerts are
independent of any browser being open. A high-temperature alert appears in the
**Alerts tab**, not on the Dashboard -- the Dashboard is a live temp/status view.
Alerts **stay until an operator dismisses them**: when the machine cools, the
*episode* is closed but the card remains, and the next hot spell raises a **new**
alert beside it rather than overwriting the old one's numbers. Alerts are
machine-scoped: an operator only sees, and is only badge-counted for, machines
within their scope.

### Audit log

Every command issued, machine merged or deleted, package deployed, account or
permission group edited, setting changed and backup key touched is written to an
append-only `audit_log` table, and read back on the **Audit Log** tab. Entries
are never rewritten or pruned; with command signing gone, this trail is the
accountability control.

Each entry carries a level -- `info` (routine bookkeeping), `notice` (an operator
changed fleet state) or `security` (identity, secrets, code execution, remote
access). Two capabilities gate the tab: **View audit log** opens it and shows
info + notice entries, and **View security audit entries** additionally reveals
the security ones. The second does nothing on its own, and the split is applied
in SQL -- a withheld entry never reaches the browser. Unlike the rest of the
console the audit log is *not* machine-scoped: most entries have no machine, so
the capability is the whole perimeter.

The tab searches across actor/action/target, filters by actor, level and date
range, pages through history, and expands any entry to its recorded detail.

Data is persisted to `logs/temp_v2.db` (SQLite) with optional CSV archiving;
rotated log files also live under `logs/`. The code lives in `hub/`; `.env` and
`logs/` sit one level up in the install root (`STATE_ROOT`), so a self-update that
mirrors the code directory can never touch operator state. Run it via
`hub/wsgi.py` (`wsgi:application` under waitress), or directly with:

```powershell
python hub/app.py
```

### Sign-in setup (OIDC)

Viewing the dashboard (`/`, `/machine/<name>`, and the `/api/machines`,
`/api/machines/<name>` endpoints, plus live Socket.IO updates) requires signing in. Sign-in succeeds for
an address in `ALLOWED_EMAILS` (break-glass superusers), one that belongs to at
least one permission group, or one whose provider says it is in a **directory group**
some permission group maps (see [Permission groups](#permission-groups)); anyone else
is refused at the callback with 403 rather than admitted to an empty dashboard. `POST /api/report` is intentionally exempt so
agents never need credentials.

Sign-in is **OpenID Connect**, and any OIDC provider will do. Configure Google,
another issuer, or both — the hub refuses to start with none.

Always required, whichever provider you use:

```
FLASK_SECRET_KEY=a-long-random-string   # signs the session cookie
ALLOWED_EMAILS=you@example.com,teammate@example.com
HUB_URL=https://your.domain.com         # public URL of this hub
SESSION_LIFETIME_DAYS=7                 # optional; see "Staying signed in" below
```

**Google.** In the [Google Cloud Console](https://console.cloud.google.com/apis/credentials),
create an **OAuth 2.0 Client ID** (Application type: Web application) and add the
redirect URI `https://your.domain.com/auth/callback` (plus
`http://localhost:3001/auth/callback` for local dev). Then:

```
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-client-secret
```

**Any other OIDC provider** (Microsoft Entra ID, Okta, Authentik, Keycloak,
Auth0…). Register a web/confidential application with the redirect URI
`https://your.domain.com/auth/oidc/callback`, then:

```
OIDC_DISPLAY_NAME=Microsoft              # what the sign-in button says
OIDC_ISSUER=https://login.microsoftonline.com/<tenant-id>/v2.0
OIDC_CLIENT_ID=...
OIDC_CLIENT_SECRET=...
OIDC_SCOPES=openid email profile         # optional
```

The issuer's `/.well-known/openid-configuration` is discovered automatically, so
there is no per-vendor code and adding a provider is configuration, not a release.
(Set `OIDC_METADATA_URL` instead if your provider's discovery document isn't at
the conventional path.)

**You can also edit all of the above from the console**, at **Settings → Sign-in** —
`.env` is only needed to bootstrap the first provider. Changes are written to `.env` and
applied immediately, with no restart. Client secrets are write-only: the editor shows
whether one is saved, never what it is, and leaving the placeholder untouched keeps it.

> ⚠️ **Only the break-glass admins in `ALLOWED_EMAILS` can change sign-in settings** —
> deliberately not `manage_settings`, and not any permission group, however privileged.
> Whoever configures the identity provider can point the hub at an issuer they control and
> sign in as anyone, so this is the one thing that stays with the accounts that already
> hold total access. A group holding *every* capability is still refused.

Guards, because this is the way back in:

- A configuration that would leave **no working provider is refused** — a hub with none
  can only be fixed by editing `.env` on the server.
- A **half-filled provider is refused**, not silently ignored, so a client ID with no
  secret is an error rather than a button that never appears.
- The **issuer must be https**. The discovery document names the token endpoint and the
  signing keys, so anything that can rewrite it in flight chooses who the hub believes
  you are.
- If the new settings are rejected when the clients are re-registered, **everything is
  rolled back** — `.env`, the live process, and the running clients — before the error
  comes back.
- Every change is audited by **field name only**; secrets never enter the database.

**Identity.** Whichever provider is used, the **email** is the identity, and it goes
through the same permission groups and the same break-glass `ALLOWED_EMAILS` list.
Some providers don't send an `email` claim — Entra often doesn't — so
`preferred_username` and `upn` are accepted as fallbacks *when they look like an
address*; a bare username is refused rather than allowed to collide with a granted
mailbox name. A provider that reports `email_verified: false` is refused. A provider
that omits the claim entirely is trusted, because you configured it.

> ⚠️ **Two providers are two doors to the same rooms.** An operator's access is
> whatever the *weaker* issuer will assert about their address. Don't enable a
> second issuer that lets users self-assert an email you've granted access to.

### Staying signed in

Sessions are **persistent and rolling**: closing the browser doesn't sign you out, and
every request pushes the expiry back, so day-to-day use never hits a login prompt. A
browser left untouched for `SESSION_LIFETIME_DAYS` (default **7**) has to sign in
again. The expiry lives inside the signed cookie, so it can't be extended client-side.

This is a security control, not just convenience — a console session can run code as
SYSTEM on any enrolled machine (see below). Shorten it if operators sign in from
machines they don't control; `/logout` always ends a session immediately.

## Permission groups

Signing in is one gate; **what you can then do is a second one**, enforced per
endpoint in [hub/permissions.py](hub/permissions.py) (model) and
[hub/permissions_web.py](hub/permissions_web.py) (the shared `access` object and the
admin API). Administered at `/permissions`.

A **permission group** carries a set of capabilities and a machine scope. An operator
gets the union of every group they're in. Scope is either an explicit machine list
(`list`) or the whole fleet (`all`).

| Capability | Grants |
|---|---|
| `view` | see these machines, their history and their command results |
| `view_audit_log` | read the audit log (info + notice; **not** machine-scoped) |
| `view_security_audit` | additionally see security-level entries — does nothing alone |
| `issue_commands` | run scripts and restart/shutdown/install — **code execution as SYSTEM** |
| `remote_control` | start a remote view/control session |
| `deploy_packages` | schedule software deployments |
| `manage_backups` | configure backups and trigger restores (deliberately fleet-wide) |
| `manage_settings` | change hub settings; delete/merge machines, pin sensors, dismiss alerts |
| `manage_users` | edit the registered-users directory (a profile directory, not access) |
| `manage_permission_groups` | create and edit groups — i.e. grant anyone, including themselves, any of the above |

> ⚠️ **`ALLOWED_EMAILS` bypasses all of it.** It is the break-glass superuser list:
> every capability over every machine, and the hub refuses to start with it empty.
> Keep it to the smallest possible set of accounts and put everyone else in a group.

### Granting a group by directory group

A permission group can also name **directory groups** — Entra security groups, AD
groups, or whatever else your issuer asserts — instead of listing operators by email.
Anyone whose sign-in carries a matching group gets that permission group, so a new
hire is granted access by being put in the right group in your directory, and never
touches the FleetHub console at all.

Add them in the group editor's **Directory groups** field. Paste whatever your
provider sends: Entra sends group **object IDs** (GUIDs), on-prem/ADFS issuers send
distinguished names, some send plain names. Matching is exact but case-insensitive —
nothing is prefix- or substring-matched, so `CN=Hospital` will not open a group named
`CN=Hospital IT`.

To find out what your provider actually sends, sign in and open the group editor: the
tokens **your own** sign-in carried are listed under the field, and clicking one adds
it. (Shown only to people who can edit permission groups.)

Configure your issuer to send the claim first — the hub reads `groups`, `roles` and
`wids`. For Entra, that means adding a **groups claim** to the app registration
(App registration → Token configuration → Add groups claim); without it the ID token
carries no groups and every mapping matches nothing.

Three things worth knowing before you rely on this:

- **Membership is read once, at sign-in.** Removing someone from a directory group
  takes effect the next time they sign in, not immediately — so revoking urgent access
  means removing them from the group *and* waiting out `SESSION_LIFETIME_DAYS`, or
  shortening it. The same is true in reverse: a newly added mapping or membership
  needs a fresh sign-in.
- **Only groups this hub maps are remembered.** The session records the mappings it
  matched, not every group you're in — a user in 200 Entra groups would otherwise
  overflow the signed session cookie.
- **Entra stops sending the claim past ~200 groups.** It sends a Graph pointer
  instead, which this hub does not follow. Such an account is refused with a message
  saying so; grant it by email address instead.

`/permissions` shows a group's mappings beside its members, but it cannot list *who*
is in a mapped directory group — the hub never queries your directory, it only
believes what an issuer signs at sign-in. Your directory remains the place that
answers "who has this access?".

### Scoping a group by Active Directory OU

With **Active Directory sync** on (below), a group's machine scope can be *"machines in
an AD OU"* instead of an explicit list. Machines then follow the directory: a PC re-filed
into a scoped OU joins the group at the next sync, and one moved out leaves it. **Nested
OUs are included** — scoping to `OU=Clinical` covers `OU=Ward 3,OU=Clinical` — and
matching is component-wise, so `OU=Clinical` never captures `OU=NotClinical`.

The group editor shows which machines the OUs currently resolve to, because an OU scope
is a rule and the only way to check you picked the right one is to see its effect.

## Active Directory sync

Optional, off by default, and **nothing contacts a domain controller until you turn it
on**. When enabled, the hub binds to a DC over LDAPS with a read-only service account and
reads **computer objects**, so each machine record gains its distinguished name, OU,
object GUID, owner (`managedBy`) and AD-recorded OS. That is what makes OU-based scoping
above possible.

Configure it at **Settings → Active Directory**:

| Setting | |
|---|---|
| `directory.enabled` | master switch; off by default |
| `directory.server` | `ldaps://dc1.corp.local` |
| `directory.base_dn` | where to search, e.g. `OU=Computers,DC=corp,DC=local` |
| `directory.bind_dn` | a **read-only** service account |
| `directory.sync_interval_minutes` | 60 by default |
| `directory.computer_filter` | `(objectClass=computer)` by default |

The bind **password** goes in `.env`, never in hub settings — the settings table is
readable by anyone with `manage_settings` and is included in the hub database backup:

```
DIRECTORY_BIND_PASSWORD=...
```

Sync needs the `ldap3` package (`pip install -r hub/requirements.txt`). It is imported
lazily, so a hub that never enables AD does not need it installed; if the feature is on
and the library is missing, the status card says exactly that.

**Press "Sync now"** after configuring — it runs a pass synchronously and reports what it
found, so a wrong bind DN takes seconds to diagnose instead of an hour.

Things worth knowing:

- **Nothing is ever written to your directory**, and no machine records are created from
  it. AD computers with no agent installed are counted and otherwise ignored — inventing
  machines from AD would fill the console with rows that never report.
- **A managed machine with no computer object raises a review alert** (`ad_unmatched`),
  auto-resolved when it reappears. Usually it means a PC was never domain-joined, was
  renamed, or is half-decommissioned. Turn it off with
  `directory.alert_on_unmatched`.
- **A machine that leaves AD has its AD fields cleared**, deliberately. A stale OU left
  behind on a deleted computer account would keep granting access through an OU-scoped
  group indefinitely.
- **Plain `ldap://` is refused by default** — a simple bind sends the service account's
  password in cleartext. There is an explicit opt-out for isolated labs.
- Searches are **always paged**. AD's default server limit is 1000 results and it
  truncates *silently*, which would look like half your fleet vanishing from the
  directory.

> ⚠️ **Removing a machine from an OU removes access at the next sync, not instantly.**
> OU scope is as current as your last sync (hourly by default). For urgent revocation,
> change the group rather than the directory.

The UI hides buttons an operator can't use, but that's presentation — the server-side
gate on each endpoint is the control, and `/api/permissions/me` deliberately reveals
nothing the caller doesn't already hold.

Two adjacent pages: `/users` (the registered-users directory — profiles, auto-created
on first login, `manage_users`) and `/inventory` (fleet-wide hardware/asset view).

## Fleet command channel (RMM)

Beyond telemetry, the hub can queue **commands** for a machine that its agent
pulls and executes (restart, rename, install, etc.). This is the hub→agent
direction, added by [fleet.py](hub/fleet.py) (core logic) and
[fleet_web.py](hub/fleet_web.py) (HTTP surface), with state in the same SQLite DB
(`agents`, `commands`, `command_results`, `audit_log`). The C#/.NET agent
(`agent/`) implements the client side of this channel. The removed Python companion
never did — it was telemetry-only, which is why it was replaced.

**Security model.**

- **Agent enrollment**: an agent presents a shared `AGENT_ENROLLMENT_SECRET` to
  `POST /api/agent/enroll` and receives a per-agent bearer token (only its hash
  is stored). All other agent endpoints require `Authorization: Bearer
  <agent_id>:<token>`. With the secret unset, no agent can enroll (fail closed).
- **Issuing a command requires a signed-in session holding `issue_commands`, plus the
  target machine in that operator's scope** — no offline signature. Every type,
  including `run_script`, which runs arbitrary PowerShell **as SYSTEM**, dispatches on
  that alone. See [Permission groups](#permission-groups) below.

  > ⚠️ **`issue_commands` is the entire perimeter for remote code execution as SYSTEM
  > on the machines in scope**, and **`ALLOWED_EMAILS` — the break-glass superuser
  > list — holds it over every machine, unconditionally.** Treat adding an address to
  > `ALLOWED_EMAILS` as granting domain-admin-equivalent power, keep those accounts on
  > MFA, and give day-to-day operators a permission group instead.

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

### The Terminal tab (interactive console)

A machine's **Terminal** tab is a **real Windows console** on that box — a pseudoconsole
(ConPTY) hosting `powershell.exe` or `cmd.exe`, rendered in the browser by xterm.js. Enter,
Ctrl-C, Tab completion, arrow keys, colour, progress bars and interactive prompts
(`Read-Host`, `$Host.UI.PromptForChoice`) all behave as they would sitting at the machine,
which is what makes it possible to drive something like `install.ps1` remotely.

That is a different mechanism from `run_script`, which still exists and is unchanged —
favorites and automation use it, because a one-shot script has an exit code and a result
worth recording. A terminal has neither. The distinction runs all the way through:

| | `run_script` | Terminal (`shell_open`) |
|---|---|---|
| Unit | one script, one result | a continuous byte stream |
| Transport | the command queue (`commands`, `command_output_chunks`) | its own `pty_*` tables (`hub/terminal.py`) |
| Retention | capped at 256 KB, kept as the durable record | rolling window, nothing retained |
| Latency | seconds | ~250–400 ms echo |
| Recorded | full output + exit code | only *that a terminal was opened*, in `audit_log` |

**A session outlives the page.** Start a download, go to Packages, come back — the shell is
still there, with its working directory, its variables, and everything it printed while you
were away. The console re-attaches to the existing session rather than opening a second
one, and replays the hub's buffer into a fresh terminal to restore the scrollback. A
session ends only when you close its tab, the shell itself exits, or a reaper fires.
**Clear** drops the scrollback on the hub as well as locally, so a cleared terminal stays
cleared when you come back to it.

**Several consoles at once.** The tab strip above the terminal lists every console you have
open on that machine — **+** opens another (up to four per operator per machine, as the hub
enforces); **×**, or a middle-click on the tab, ends one. Each tab is its own shell with its own working directory and
scrollback; the front one polls at typing speed and the rest keep collecting output in the
background, so a build in one tab keeps running while you work in another. The strip is
built from the hub's list of *your* sessions rather than from anything the browser
remembers, so a console you opened on one computer is listed — with its scrollback — when
you sign in from another, or from a second browser tab, or after a browser restart. The
**Shell** dropdown chooses what the next **+** opens and follows whichever tab is in front;
changing it no longer ends anything. A shell that exits on its own leaves its tab behind,
struck through, showing its last screen until you dismiss it.

**What is and isn't recorded.** Opening a terminal writes a `shell_open` row to the
`audit_log` naming the operator, the machine and when — the same accountability trail as
any other command. What is *typed* is deliberately not kept: the stream lives in a rolling
buffer (256 KB) that is dropped when the session ends. A transcript of a SYSTEM console
would be a store of half-typed credentials, and nothing reads it back.

**Isolation.** A session is bound to one operator and one machine. Another operator with
`issue_commands` on the same machine may open their own terminal but cannot read, type
into, or close somebody else's — "can run commands here" is not consent to watch someone's
keystrokes. On the agent side each session is checked against the authenticated agent's own
machine.

**Cleanup.** An open session is a live SYSTEM shell, and because sessions now persist across
navigation, "still open" no longer implies "still wanted". Two different silences are
measured separately, and conflating them would disable one of them entirely:

- **The agent went quiet** (15 min). It polls for keystrokes continuously while it holds a
  session, so a gap means the machine is gone.
- **Nobody came back** (60 min). Measured on a clock that *only the console's own polls*
  refresh — the agent's polling would otherwise keep a session looking alive forever.

The agent keeps a much longer backstop timer of its own (2 h) for a hub that forgets a
session while still answering, and the pseudoconsole's child is enlisted in a
kill-on-job-close job so even the hard `Environment.Exit` a self-update uses cannot orphan
one.

**Rollout.** Needs **agent 3.15.0+**. Below that the tab falls back to the older
line-oriented terminal (`fleet-terminal.js`), which sends whole scripts and prints the text
that comes back — usable, but it cannot answer an interactive prompt. The console picks the
mode from the agent's reported version, so a fleet mid-update gets whichever each machine
can actually do.

Add to the hub's environment / `.env`:

```
AGENT_ENROLLMENT_SECRET=a-long-random-shared-secret
```

**Endpoints.** Agent-facing (token auth): `POST /api/agent/enroll`,
`POST /api/agent/heartbeat`, `GET /api/agent/commands` (pull + claim),
`POST /api/agent/commands/<id>/result`, `POST /api/agent/commands/<id>/output`
(live output streaming), and the terminal's `GET|POST /api/agent/pty/<session>/input`
`/output` `/closed`. Console-facing (`issue_commands` + machine scope):
`GET /api/fleet/status` (online/offline), `GET|POST /api/fleet/commands`,
`GET /api/fleet/commands/<id>`, `GET /api/fleet/commands/<id>/output`,
`GET|POST|DELETE /api/fleet/favorites[/<id>]` (saved scripts), and
`POST /api/fleet/pty` + `/api/fleet/pty/<session>/input` `/output` `/clear` `/close`.
Every issue/claim/complete/enroll is written to `audit_log`.

### The Processes card (the machine's task manager)

A machine's **Overview** tab carries a **Processes** card: every process grouped by name
with its CPU and memory, expandable to the individual instances, with **End task** and
**Restart** on each. Core logic in [processes.py](hub/processes.py), HTTP surface in
[processes_web.py](hub/processes_web.py), UI in `hub/static/js/processes.js`, agent side in
`Telemetry/ProcessReader.cs` and `Fleet/Executors/ProcessExecutors.cs`.

**Reading is `view`; ending or restarting is `issue_commands`.** What is running on a PC is
inventory in the same sense its disks and its sensor tree are, and anyone who can open the
machine page already reads both. Ending a process is strictly less dangerous than the
`shutdown` the command gate already covers, so there is no new capability.

**The card is collapsed by default, and that is load-bearing.** No machine samples its
processes until an operator opens it. Opening it registers a *watch*; the hub answers that
machine's next heartbeat with `processes_wanted: true`; the agent then samples every 5s and
the list rides the heartbeat it already sends. The watch lapses ~45s after the operator
navigates away, so a fleet nobody is looking at does no process work and sends nothing. The
first list therefore appears within about 15 seconds rather than instantly — the card says
so instead of spinning.

**Ending a process is guarded twice, against the same hazard.** Every action carries the
process NAME the operator saw alongside its PID, because a rendered list is always a few
seconds old and Windows recycles process ids within minutes — a bare PID is an instruction
to kill whatever now holds that number. The agent re-reads the live process and refuses a
mismatch rather than resolving it. **Critical Windows processes are refused outright**
(`csrss`, `wininit`, `winlogon`, `services`, `lsass`, `smss`, the kernel pseudo-processes,
and the agent itself): ending one is a bugcheck, not a closed program. `svchost` is refused
too — killing a service host takes every service in it down, and for the RPC host that
reboots the machine — with the refusal pointing at Restart, which does the right thing.
`explorer.exe` is deliberately killable.

**Restart means three things, decided on the machine.** A process hosting exactly one
Windows service is restarted *as that service* (stop, wait, start, wait, and its running
dependents brought back) rather than killed, so the SCM does not record a crash. One hosting
several is refused with them named. Anything else is relaunched from its own image, in the
Windows session it was running in, **as the user who was running it** — not as SYSTEM, which
would give the program no profile and no access to the documents it exists to open.

**Endpoints** (console-facing): `GET /api/machines/<machine>/processes` (`view` + scope;
polling it is what renews the watch), `POST /api/machines/<machine>/processes/kill` and
`POST /api/machines/<machine>/processes/restart` (`issue_commands` + scope). Both queue an
ordinary fleet command and answer with its id, which the card follows through
`GET /api/fleet/commands/<id>` — so every End task and Restart lands in `audit_log` with the
operator, the machine, the process name and the PID. `kill_process` and `restart_process`
are deliberately refused by the generic `POST /api/fleet/commands` endpoint and cannot be
saved as favorites: the guards above live on the dedicated route, and a saved PID is a
different process by tomorrow.

## Package deployment (PDQ-style)

Define an installer once — payload, silent command line, what proves it worked — then
aim it at machines and watch it land. Core logic in [packages.py](hub/packages.py), HTTP
surface in [packages_web.py](hub/packages_web.py), UI at `/packages`, agent side in
`agent/src/TempMonitorAgent/Fleet/Executors/DeployPackageExecutor.cs`. State lives in the
same SQLite DB (`packages`, `package_sources`, `deployments`, `deployment_targets`).

**A package is a recipe plus its payloads.** A payload is either a file uploaded to the
hub (stored beside the database under `logs/packages/`, content-addressed by SHA-256 and
shared between packages built from the same installer) or an external reference — a
winget id, an `https://` URL, or a UNC path. The recipe is the command line (with
`{file}` standing in for the resolved payload), a timeout, the accepted exit codes, and a
detection rule.

**A recipe can also be several steps.** One command line covers an MSI and not much else;
a driver pack is download a zip, unpack it, hand the folder to `pnputil`. So a package may
instead carry an ordered list of steps, each with its own timeout and accepted exit codes:

| Step | What it does |
|---|---|
| `run` | an executable with arguments — an MSI, a vendor `setup.exe` |
| `powershell` | an inline script as SYSTEM; the escape hatch for copy / registry / services |
| `winget` | `winget install --id …`, with its own trust chain |
| `extract` | unpacks a `.zip` into a folder later steps can point at |
| `pnputil` | stages and installs a folder of `.inf` files |

Payloads are **named slots**, so steps say which file they mean: `{drivers}` is the
payload named `drivers`, `{work}` is the attempt's own directory, and an `extract` step
binds its output folder for the steps after it. `{file}` still means the payload when
there is exactly one, so nothing written before steps needs changing. A variable nothing
provides — including one a *later* step would bind — is refused when the package is saved
rather than reaching a machine as a literal brace pair; the grammar is `{lowercase_words}`
only, so a literal MSI product code like `{90160000-008C-0000-1000-0000000FF1CE}` passes
through untouched. A payload no step ever opens is refused for the same reason `{file}`
has always had to appear somewhere.

The first step that fails stops the deploy, unless it is marked "carry on". Detection runs
once at the end — it is a claim about the package, not about a step. `pnputil` steps
default to accepting `{0, 259, 3010}`: 259 is a driver install that simply ran out of INFs,
and failing it would paint a clean driver rollout red the way failing 3010 would paint a
fleet of MSI installs red.

> **Steps need agent 3.22.0 or newer.** An older agent reads only the single-command
> fields, so the hub sends a step-based package with no command for it to run: it fails the
> deploy with "no install command" and the target retries until the update reaches it.
> Packages written as one command still go out in the old shape and run on every agent in
> the field.

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
can be hash-pinned too; winget has its own trust chain. The agent works in one directory
per attempt under `%ProgramData%` (SYSTEM-owned, not `%TEMP%`, so a half-finished install
cannot leave an executable where a standard user could swap it out first) and deletes it
whole on every path. `extract` refuses any archive entry that would land outside that
directory.

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

**If an upload fails with HTTP 413, it is not the hub.** The hub's own limit is
`deploy.max_upload_mb` (512 MB by default) and it is enforced in Python, so exceeding it
gives you a JSON error naming the limit. A raw `413` means the TLS terminator in front of
the hub rejected the body before Flask ever saw it — and **nginx's default
`client_max_body_size` is 1 MB**, so the very first real installer anyone uploads hits it.
Raise it to something above your largest payload and reload:

```nginx
server {
    client_max_body_size 600m;   # >= deploy.max_upload_mb
    # A large upload over a slow link can also hit the read timeout:
    proxy_read_timeout 300s;
    proxy_request_buffering off; # stream to the hub instead of buffering the whole file
}
```

The equivalents elsewhere: IIS `maxAllowedContentLength` (default ~30 MB) *and*
`maxRequestLength`; Apache `LimitRequestBody`; Cloudflare's proxy caps free plans at
100 MB and it cannot be raised from the origin.

## Backups

A consistent snapshot of the hub database, compressed, encrypted **on the hub** and pushed
offsite on a schedule. Core logic in [backups.py](hub/backups.py), HTTP surface in
[backups_web.py](hub/backups_web.py), UI at `/backups`, and the restore tool at
[restore_backup.py](hub/restore_backup.py). State lives in the same SQLite DB
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
> python hub/restore_backup.py --in 20260721T030000Z-temp_v2.db.gz.fhb --out temp_v2.db --verify
> python hub/restore_backup.py --in <file>.fhb --info     # just read the header
> ```
>
> Then stop the hub service, move the old `logs/temp_v2.db` aside (with its `-wal`/`-shm`),
> drop the restored file in, and start the service. `--verify` runs
> `PRAGMA integrity_check` first, which is worth the seconds.

**Destinations: S3-compatible or WebDAV, your choice per destination.** S3 covers AWS,
MinIO, Backblaze B2 and Wasabi — signed with SigV4 implemented in ~100 lines of stdlib
`hmac` rather than pulling ~80 MB of botocore onto a hub whose whole install is ~2 MB,
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
`PUT /api/backups/schedule`, `POST /api/backups/run`, `GET /api/backups/machines`,
`POST /api/backups/preview` (resolve path patterns against a real machine),
`POST /api/backups/files/run` and `POST /api/backups/files/cancel` (fleet-wide
"back up now" / cancel, over the caller's scope).

The per-machine routes need `manage_backups` **and** that machine in scope:
`GET|PUT /api/backups/machines/<machine>`, `GET /api/backups/machines/<machine>/manifest`,
`POST /api/backups/machines/<machine>/run`, `POST /api/backups/machines/<machine>/cancel`,
`POST /api/backups/machines/<machine>/restore`,
`GET /api/backups/machines/<machine>/restores`.

Agent-facing (token auth): `POST /api/agent/backups/upload/<run_id>`,
`POST /api/agent/backups/<run_id>/result`, and for restores
`GET /api/agent/backups/restore/<id>/plan`, `.../archive/<index>`, `.../result`.

> Revealing the key is a **POST with a JSON body**, not a GET — so it cannot be triggered
> by a link, an `<img src>`, or anything else a browser fetches on someone's behalf. Keep
> it that way.

Tunables live in Settings under **Backups**, or on the Backups page itself: on/off,
destination, interval, and generations to keep.

### Per-PC file backups

Configured on the Backups page's **Backup Settings** tab, per machine on that machine's
**Backup** tab. Path selection lives in [backup_paths.py](hub/backup_paths.py).

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
> python hub/restore_backup.py --in 20260721T030000Z-a1b2c3-000-full.fhb --list
> python hub/restore_backup.py --in <file>.fhb --extract C:\Recovered
> python hub/restore_backup.py --in <file>.fhb --extract C:\Recovered --match "*/Desktop/*"
> ```
>
> One master key opens every machine: the per-machine key is derived from it, and the
> envelope header says which machine to derive for.

> **Status:** built end to end, shipped in hub 1.30.0 / agent 3.9.0 and signed-released
> since — the fleet has it. See [ROADMAP.MD](ROADMAP.MD).

## Remote view & control

Live remote view **and control** of a managed PC over WebRTC (H.264), from the machine
page's **Remote** tab. Gated on the `remote_control` capability plus the machine being in
the operator's scope; every session start/stop is in the audit log.

- **How it works.** The agent runs as SYSTEM in session 0, which has no desktop to capture,
  so on session start it injects a helper (the same signed agent binary, `--remote-helper`)
  as SYSTEM into the interactive session. The helper captures the screen (DXGI), encodes
  H.264, and streams it to the operator's browser over WebRTC; the browser sends mouse and
  keyboard back over a data channel (`SendInput`). Ctrl+Alt+Del is supported.
- **Which session gets captured — and choosing it.** The **Session** picker in the Remote page's viewer
  lists the machine's logon sessions (who, where, what state), reported on the agent heartbeat.
  Leave it on **Auto** and the agent picks: a session Windows reports as `WTSActive`, not merely
  the physical console — on a machine administered over RDP the console session is often signed
  out and has no rendered desktop at all. The console wins ties, so a physically-present user
  beats a stray RDP session. Failing that, it falls back to the **console session at the logon
  screen**, which is what makes signing in remotely to a machine nobody is signed into possible
  at all. Injecting into the wrong session is not an error anyone notices: the helper starts,
  Desktop Duplication finds nothing to duplicate and falls back to GDI, and the operator gets a
  perfectly healthy stream of **a black screen**. The helper logs which tier it chose and its
  own `session=` at startup.
- **The lock screen, the logon screen, and UAC.** The helper follows the Windows *input
  desktop*, re-attaching its capture and input threads whenever it switches (`Default` ↔
  `Winlogon`) and rebuilding Desktop Duplication on the new desktop. That is what makes the lock
  screen visible and typeable. Signing in destroys the Windows session the helper lives in, so
  the service supervises the helper and re-injects it into the new session — the view comes back
  on its own a few seconds after the remote login succeeds.
- **Auto follows the session; a pinned session stays pinned.** Sign-out and *switch user* move
  the interactive desktop into a different Windows session, and only sometimes by killing the
  one the helper is in. On a switch-user the old session survives, disconnected but intact, so
  the helper keeps streaming a live picture of a desktop that is no longer on the monitor and no
  longer where the operator's keystrokes land. With **Auto** selected the service re-asks which
  session is interactive every few seconds and moves the helper when the answer changes (the
  browser reconnects on its own, as it does after a sign-in). A session the operator picked by
  hand is never moved — watching one user's session while somebody else uses the console is a
  legitimate thing to be doing.
- **A screen nobody is touching is not a broken capture.** Desktop Duplication reports "no new
  frame" for a desktop that has not changed, which is the permanent state of a logon screen.
  Treating that as a failure is how the remote view used to end up black with *"the agent is not
  getting any frames"* on a machine whose monitor was showing the logon prompt perfectly well —
  the pipeline discarded the frame it already held, and its fallback to GDI (which cannot see
  the secure desktop at all) guaranteed black. The pipeline now keeps the picture it has, only
  reports a stall when it has never had one, and only leaves Desktop Duplication when the
  duplication itself cannot be built.
- **Headless machines need a virtual display.** Desktop Duplication duplicates a display
  *output*; a machine with no monitor has none, so there is genuinely nothing to capture and the
  stream is black. The viewer shows a **No display outputs** badge on such a machine and
  offers to install a bundled IddCx **virtual display driver** on demand, per machine (never
  fleet-wide, never automatically). It is a user-mode (UMDF2) driver, so no test-signing, Secure
  Boot or HVCI changes are involved — but installing it does add its publisher to that machine's
  TrustedPublisher store, which is why the command output names the exact subject and thumbprint
  and the action is audited at security level. Set the payload once: upload the driver's
  "Driver Only" zip on the **Packages** page, then pin it under **Settings → Remote Control**.
  Uninstall is available from the same panel, and `monitors: 0` stands the driver down without
  removing it once a real monitor is plugged in.
- **Stream controls.** Session, codec (H.264 or VP8) and encoder (auto / hardware / software)
  are chosen **before** connecting — they are negotiated in the SDP or decide which encoder gets
  built, so changing them needs a new session, and the UI disables them while connected rather
  than letting a change silently do nothing. Monitor, quality preset (or raw fps / bitrate /
  resolution scale), **fullscreen** and **view only** change **live** mid-session over the
  control data channel. View-only is an accident guard on the viewer, not a permission: it stops
  *this browser* sending input, and does not stop anyone else driving the machine.
- **Consent** is `unattended` by default (connects immediately, standard RMM) or `attended`
  (the logged-in user must approve first). Set it in **Settings → Remote Control**.
- **TURN.** Agents sit behind arbitrary NATs, so WebRTC media usually needs a TURN relay.
  **The hub is the TURN server by default** (coturn from [`turn/`](turn/README.md)), but it does
  not have to be — the relay can live on any Windows box you can forward ports to. Install it
  there on its own with *Install TURN* (`-Component Turn`) and point
  **Settings → Remote Control** at it; the questions, preconditions and verification are
  identical either way. The **hub installer sets this up for you** when you want both on one
  machine: choose *Install Hub*, answer yes to "Configure this hub as the
  TURN/STUN server", and it generates `REMOTE_TURN_SECRET`, seeds
  **Settings → Remote Control → TURN/STUN servers** to `turn:<host>:3478` / `stun:<host>:3478`,
  and then builds the relay itself. On Windows that means a **dedicated `FleetHubTurn` WSL2
  distro running coturn natively** under systemd — created, configured, firewalled (both the
  Windows *and* Hyper-V firewalls), given a boot task, and then **verified with a real TURN
  Allocate** before the installer finishes. Any secret it generates is printed **once** at the
  end — save it. The hub mints short-lived per-session credentials from it (nothing per-user).
  Needs Windows 11 22H2+, the Store WSL package, and ~3 GB free; the installer checks each and
  skips TURN with an explanation rather than half-configuring anything.
- **Managing TURN from the UI.** The whole config lives in **Settings → Remote Control**: the
  STUN/TURN URLs, consent mode, TTLs, and a **TURN status card** that shows whether
  `REMOTE_TURN_SECRET` is set and — the number that predicts a working session — how many ICE
  servers a session hands a peer right now (`0` is the "agent logs `ice_servers=0` → peer
  failed" case). From that card an admin (`manage_settings`) can **set or rotate the secret**
  without shell access; it writes `.env` and applies live. Because coturn validates against
  its own copy, **a rotation is only half done until you sync it** — the new value is shown once
  so you can put it in the relay's config (`/etc/turnserver.conf` in the WSL distro, then
  `systemctl restart coturn`; `turn/.env` for a Docker relay). Leaving the secret unset simply
  omits TURN (STUN/direct paths only — fine on a LAN). See [turn/README.md](turn/README.md) for
  ports, the public-IP requirement, and the host-OS notes.
- **Why the Windows installer builds a WSL distro rather than running a container.** A coturn
  *container* on Windows relays from the Docker bridge. `--external-ip` makes it advertise the
  public address and inbound DNAT works — so both peers allocate and the logs look healthy — but
  relay→peer egress is SNAT'd to an arbitrary source port, and ICE requires the peer to receive
  from *exactly* the advertised candidate. Every check fails and the agent logs
  `peer connection state: failed`. A WSL distro in mirrored networking mode shares the host's
  network namespace, so the relay ports stay symmetric. `docker-compose.windows.yml` is kept for
  LAN and credential testing only. [turn/README.md](turn/README.md#host-os-notes) has the full
  forensics and a symptom-ordered troubleshooting table.
- **Debugging a session that won't connect.** Read the two logs together: the agent's
  `C:\ProgramData\FleetHub\Agent\remote-helper.log` and coturn's. `ice_servers=0` means TURN
  isn't configured; allocations incrementing **twice** in coturn's log means both peers reached
  and authenticated against the relay, which rules out reachability, the shared secret and the
  credential scheme in one stroke. Always confirm with a machine on a genuinely different
  network — a LAN test succeeds on host candidates alone and proves nothing about TURN.

- **Diagnosing capture before you involve a browser.** Two agent-binary self-tests, both
  runnable on the machine itself: `--desktop-probe [seconds]` prints the input desktop as it
  changes and whether this process can attach to it (lock the screen, trigger a UAC prompt), and
  `--remote-capture-test <file.h264> <seconds> [monitor] [fps] [kbps] [encoder]` writes a playable
  clip straight from the capture and encode path with no hub, browser or session injection
  involved. `encoder` (auto / hardware / software) pins the H.264 MFT flavour, which separates a
  hardware encoder that will not run on this machine from a broken capture pipeline.

> **Status:** built — shipped in hub 1.44.0 / agent 3.14.0; the agent half has been
> signed-released since (current: agent 3.15.0). Deploy the hub before an agent release
> so the fleet never runs ahead of it.
>
> **On-hardware validation still outstanding**, in rough order of how much rests on it:
> 1. Whether Desktop Duplication works against the Winlogon desktop after `SetThreadDesktop`
>    on your specific GPU and driver. Everything about seeing the lock screen rests on this;
>    `--desktop-probe` answers the first half of it in a minute.
> 2. Whether IddCx and desktop-switch capture behave the same on **Windows Server** SKUs.
> 3. Whether `SendSAS` is honoured — from the service (where the agent now routes it over a
>    SYSTEM-only named pipe) or at all, given `SoftwareSASGeneration` may be Group-Policy owned.
> 4. Whether the hand-rolled SetupAPI root-device creation matches what `devcon`/`nefcon` do.
>
> Per-machine consent override is still a follow-up.

## Wake-on-LAN

Power a sleeping machine on from the console, so an out-of-hours patch window or a remote
session doesn't depend on somebody being at the desk to press a button.
[wake.py](hub/wake.py) (model) + [wake_web.py](hub/wake_web.py) (HTTP), a **Network** tab on
the machine page, a **Wake offline PCs** button on Asset Inventory, and `wake.*` settings.

**Delivery is agent peer-relay, and that is the whole design.** The hub picks an *online*
machine whose reported IPv4/prefix puts it on the same subnet as the target, and issues
**that** machine a `wake_machine` command carrying the target's MACs and the subnet
broadcast address; its agent sends the magic packet to UDP 9. A hub-sent broadcast reaches
the hub's own network segment and nothing else, so for a helpdesk with more than one site it
is the wrong default — the machines that most need waking are at the branch office the hub
has never shared a broadcast domain with. Peer-relay crosses sites and VLANs with no router
configuration at all.

The hub's own broadcast survives as a **fallback only** (`wake.hub_broadcast`), taken when
no awake peer exists *and* the hub can prove it shares the target's subnet. That covers the
real hole in peer-relay — 3am, every PC on the subnet asleep — on a single-site fleet.

**Subnets come from the agents, never from a heartbeat's source address.** Nothing in the
product collected a MAC or an IP before this. A new change-only reporter
(`Network/NicReader.cs`) sends per adapter: MAC, IPv4, prefix, link state, type, and whether
the NIC is allowed to wake the machine — into a new `machine_nics` table. It has to come
from the machine: the only address the hub can see for itself is the NAT'd site edge, which
every PC at an office shares and which would fold a whole building into one fictional subnet.

**A wake is an *attempt*, and every state name says so.** Nothing acknowledges a magic
packet, no error comes back for a MAC that does not exist, and a machine that was already
awake looks identical to one that just woke. So:

| State | What it means |
|---|---|
| `pending` | Looking for an awake PC on the target's subnet. **Survives across ticks** — a target whose subnet is entirely asleep is woken by the first peer to come online. |
| `relaying` | A `wake_machine` command is queued at a peer. |
| `sent` | The packet went out. **Not success.** |
| `awake` | The target checked in *after* the packet. The only success. |
| `already_awake` | It was on when you asked. Not an error — that is the answer. |
| `no_relay` | The deadline passed with nobody awake on that subnet. **Not a failure**: at 3am it is the expected state, and the message names the subnet so it can be acted on. |
| `no_answer` | The packet went out and the machine never checked in. A report of silence, not a claim about the packet. |
| `unwakeable` | Nothing to send to — refused before dispatch, with the reason attached. |

Confirmation compares against **when the packet was sent**, not against "is it up now": a
machine can read online on a last-contact timestamp from before the packet, and treating
that as a wake would confirm one nobody performed.

**Most of a WoL rollout is preconditions, not code**, so the Network tab names every reason
a machine cannot be woken — no wired adapter, Wi-Fi only (this mechanism cannot reach a
laptop over Wi-Fi at all), no address on record, waking turned off on the NIC, and **Windows
Fast Startup**, which turns shutdown into a hybrid state that defeats wake-from-S5 on many
machines. **Fix wake settings** (`prepare_wake`) is the remedy: it enables the device's own
"allow this device to wake the computer" via `powercfg /deviceenablewake`, sets the driver's
`*WakeOnMagicPacket` property, and turns Fast Startup off. Both NIC settings have to be on —
an adapter allowed to wake the machine but with magic-packet wake off in its driver looks
perfectly configured in Device Manager and ignores every packet. Wireless adapters are never
touched. The firmware-side enable is the [BIOS settings](ROADMAP.MD) half of roadmap #9,
which is why the two arrived together.

**Scheduled waking is the point.** With `wake.auto_wake_targets` on, the deploy and firmware
schedulers wake a maintenance window's offline targets when it opens — a window that
dispatches into a dark office installs nothing. Off by default: waking a fleet at 3am is a
decision, not a side effect of scheduling a deploy.

**Gating**: reading the adapters and the diagnosis is `view` + machine scope (it is
inventory, like a model or a disk layout); waking, preparing and cancelling are
`issue_commands` + machine scope. No new capability — waking a PC is strictly less dangerous
than the `shutdown` that gate already covers.

**Endpoints** (console-facing): `GET|POST /api/wake/machines/<machine>`,
`POST /api/wake/machines/<machine>/prepare`, `POST /api/wake/fleet`,
`POST /api/wake/requests/<id>/cancel`, `GET /api/wake/requests`. The adapters arrive on the
existing `POST /api/agent/heartbeat` under a `network` key.

> **Status:** built — hub 1.63.0 / agent 3.21.0. **The agent half ships in source and needs
> a signed release** before the fleet gains it; deploy the hub first.
>
> **On-hardware validation outstanding**: the WMI device power policy and the
> `*WakeOnMagicPacket` value real drivers publish, whether a docked laptop reports the dock's
> adapter, and whether `powercfg /deviceenablewake` matches on the description this reader
> reports. Everything else is covered by tests against literal payloads.

## Signing releases

One artifact in this repo is Ed25519-signed so a compromised hub or repo commit
can't push code that runs as admin fleet-wide unverified:

- **The C# agent** (`agent/`) — the self-update manifest `agent/agent.manifest.json`
  carries the version, the exe's sha256 and its download URL, and is signed
  byte-for-byte. See [agent/README.md](agent/README.md) for `agent/release.ps1`
  (automates the whole release: version bump, publish, GitHub release, sign, upload)
  or the manual `sign_release.py --sign-agent` steps. `.gitattributes` pins the
  manifest and its `.sig` to `-text` so git never rewrites line endings — the signed
  bytes must match exactly what the fleet downloads.

(`companion.py` was signed with the same key until it was removed from the repo.
Nothing signs a loose file anymore.)

One-time setup: `python sign_release.py --genkey`, keep the private key OFF the
repo, paste the printed public key into the agent's `AgentConfig.UpdatePublicKeyHex`.

This is the **release** trust root: it governs what *code* the fleet is allowed to run,
and it is fully enforced. It is unrelated to fleet *commands*, which are no longer signed
(see [Fleet command channel](#fleet-command-channel-rmm)). Don't conflate the two — a
compromised hub still must not be able to push a malicious binary, which is exactly what
these signatures prevent.
