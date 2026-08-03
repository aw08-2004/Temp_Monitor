# FleetHub — C#/.NET Fleet Agent

The fleet's only telemetry client: a Windows Service (runs under **LocalSystem**) that
replaced the Python `companion.py`, since removed from the repo. It reaches telemetry
parity with the companion **and** speaks the hub's fleet command channel: it enrolls,
heartbeats, polls for commands, executes them, reports
results, and updates itself from a **signed** manifest (verified fail-closed). It also
carries the client half of the interactive terminal (ConPTY), package deployment,
per-PC file backup/restore, and remote view/control.

- **Target:** .NET 10 (`net10.0-windows`), published **self-contained single-file
  win-x64** (no runtime install needed on the fleet).
- **Sensors:** LibreHardwareMonitorLib **in-process** — no separate
  LibreHardwareMonitor.exe / `:8085` web server. Needs the **PawnIO** kernel driver
  (the installer sets it up). Every LHM hardware category is enabled (CPU, GPU, RAM,
  motherboard/SuperIO, storage, NICs, fan controllers, PSU, battery) and the whole
  sensor tree is flattened into the report — fan RPM, control duty, package watts and
  voltages ride along with the temperatures. The hub decides what to chart.

## Layout
```
src/TempMonitorAgent/
  Program.cs / Worker.cs         host + main loop, executor registration
  AgentConfig.cs                 constants, endpoints, trust roots, %ProgramData% paths
  Models.cs                      wire DTOs shared across the client surfaces
  Telemetry/                     SensorReader (LHM), SystemInfo (WMI), TelemetryReporter
  Fleet/                         FleetClient, SignatureVerifier, CommandDispatcher,
                                 OutputStreamer, ProcessRunner, WingetLocator,
                                 Shell/ (ConPTY sessions), Executors/
  Backup/                        BackupFilesExecutor, RestoreFilesExecutor (VSS, chains)
  Remote/                        remote view/control helper + virtual-display executors
  Update/                        SelfUpdater, VersionUtil
  State/                         AgentState (agent.json, restart_state.json)
tests/TempMonitorAgent.Tests/    xUnit: update-manifest sig verify, versions
tests/ShellParserTests/          xUnit: terminal/ConPTY stream parsing
install/agent-install.ps1        installs the service (+ PawnIO, recovery, enroll secret)
```

## Wire protocol (must match the Python hub)
- Telemetry: `POST /api/report` (no auth). Cadence 5s temp / 10s sensors / 600s uptime.
- Fleet: `POST /api/agent/enroll` → `{agent_id, token}`; then
  `Authorization: Bearer <agent_id>:<token>` on `POST /api/agent/heartbeat`,
  `GET /api/agent/commands` (pull+claim), `POST /api/agent/commands/<id>/result`,
  `POST /api/agent/commands/<id>/output` (live output while a script runs).
- Terminal (ConPTY): `GET /api/agent/pty/<session>/input`,
  `POST /api/agent/pty/<session>/output`, `POST /api/agent/pty/<session>/closed`.
- Remote: `GET /api/agent/remote/<session>/poll`, `POST .../signal`, `POST .../ended`.
- Backups: `POST /api/agent/backups/upload/<run_id>`, `POST /api/agent/backups/<run_id>/result`;
  restores via `GET /api/agent/backups/restore/<id>/plan`, `.../archive/<index>`, `.../result`.
- Packages: `GET /api/agent/packages/<sha256>` (payload fetch, re-hashed before execution).
- Commands are **not signed**. The agent executes what an enrolled, authenticated pull
  returns; the hub authorizes on an allow-listed console session and records every
  command in its `audit_log`. (Until hub 1.10 / agent 3.1, `run_script`,
  `install_driver` and `update_bios` additionally required an offline Ed25519 signature
  verified here. No key was ever configured, so in practice they were always refused —
  which is why removing the gate is what made them work, not a loosening.)

Implemented executors (registered in `Program.cs`):

| Area | Types |
|---|---|
| Low risk | `restart`, `shutdown`, `rename`, `gpupdate`, `install_app` |
| Scripts | `run_script` |
| Packages | `deploy_package` |
| Terminal | `shell_open`, `shell_input`, `shell_signal`, `shell_reset` |
| Backups | `backup_files`, `restore_files` |
| Remote | `start_remote_session`, `install_virtual_display`, `uninstall_virtual_display`, `set_virtual_display_mode`, `refresh_remote_inventory` |

`install_driver` / `update_bios` are still registered as `StubExecutor`s.

**Still signed, and unrelated to the above:** the self-update manifest. `SelfUpdater`
verifies it with `SignatureVerifier.VerifyRaw` against `AgentConfig.UpdatePublicKeyHex`
before any binary replaces the running one. That is what stops a compromised hub from
pushing malicious code to the fleet — do not remove it.

## Configuration

Settings are read as `FLEETHUB_*`, falling back to the pre-rename `TEMP_MONITOR_*`
name — machines installed before the FleetHub rename still have the old machine-level
env vars set, and an agent that self-updates must keep honouring them or a box pinned
at a non-default hub silently swings back to the production default.

- `FLEETHUB_HUB` (legacy `TEMP_MONITOR_HUB`) — hub base URL (default
  `https://temp.arkeanos.net`).
- `FLEETHUB_MACHINE` (legacy `TEMP_MONITOR_MACHINE`) — machine name (default
  `Environment.MachineName`).
- `AGENT_ENROLLMENT_SECRET` — enrollment secret (installer writes it to
  `HKLM\SOFTWARE\FleetHub\Agent`, legacy `HKLM\SOFTWARE\TempMonitorAgent`; env
  overrides for testing).
- `FLEETHUB_NO_UPDATE=1` (legacy `TEMP_MONITOR_NO_UPDATE`) — disable self-update (testing).

State lives in `%ProgramData%\FleetHub\Agent` (`agent.json`, `config.json`,
`restart_state.json`, `update/`, `remote/`). A pre-rename
`%ProgramData%\TempMonitorAgent` is migrated on first touch, and kept if the migration
fails rather than starting from a blank identity.

## Build / test / publish
```powershell
dotnet test  agent/TempMonitorAgent.slnx
dotnet publish agent/src/TempMonitorAgent/TempMonitorAgent.csproj -c Release -o agent/dist
```

## Release + self-update

**Automated (recommended):** `agent/release.ps1` runs the whole flow — bumps the
version in `AgentConfig.cs` + the `.csproj`, publishes, creates/reuses the GitHub
release `agent-v<version>`, signs the manifest against the exact asset URL, uploads
the exe, and commits the manifest + `.sig`. Requires `gh` CLI, authenticated
(`gh auth login`), and a working `git push` from wherever you run it.
```powershell
agent/release.ps1 -Version 3.0.1 -DryRun         # print the plan, touch nothing external
agent/release.ps1 -Version 3.0.1                 # do it; prompts before pushing
agent/release.ps1 -Version 3.0.1 -Push           # do it, push without prompting
```

**Manual, step by step** (what the script above automates):
1. Bump `Version` in [AgentConfig.cs](src/TempMonitorAgent/AgentConfig.cs) and
   `<Version>` in [TempMonitorAgent.csproj](src/TempMonitorAgent/TempMonitorAgent.csproj)
   — keep them in sync.
2. `dotnet publish … -o agent/dist` (single-file exe).
3. `python sign_release.py --sign-agent --file agent/dist/TempMonitorAgent.exe \
      --agent-version <v> --agent-url <release-asset-url>` → writes and signs
   `agent/agent.manifest.json` (+ `.sig`). The `--agent-url` must exactly match where
   you upload the exe in the next step — it's baked into the signed manifest.
4. Commit the manifest + `.sig` together (pinned `-text` in `.gitattributes`) and upload
   the exe to that exact release-asset URL. The running service checks the manifest
   weekly (and on a hub `latest_version` hint), verifies the signature, hash-checks the
   binary, renames the running exe aside, drops the new one in, and exits code 17 so the
   SCM restarts onto it. **If you skip signing, fleet updates stall** — the signature
   check fails closed.

## Install (elevated PowerShell)
```powershell
agent/install/agent-install.ps1 -AgentExe .\dist\TempMonitorAgent.exe -EnrollmentSecret <secret>
agent/install/agent-install.ps1 -AgentUrl <release-url> -EnrollmentSecret <secret> `
    -HubUrl https://temp.arkeanos.net
agent/install/agent-install.ps1 -Uninstall
```
`-InstallDir` defaults to `C:\Program Files\FleetHub\Agent`. The Windows service is
still registered as **`TempMonitorAgent`** on purpose: .NET takes the service name from
the assembly, and a self-updating agent swaps its binary without re-registering, so
renaming one side alone would break exactly the machines already in the field.

Logs: `%ProgramData%\FleetHub\Agent\companion.log` — kept under that name for the same
reason as the service name and the `companion_version` wire field: it is what's already
on disk across the fleet. Plus `remote-helper.log` beside it for the remote view/control
helper (it runs in a different session, so it gets its own file).

Self-tests runnable on the machine, no hub or browser involved:
`--desktop-probe [seconds]` (which input desktop, and can this process attach to it)
and `--remote-capture-test <file.h264> <seconds> [monitor] [fps] [kbps] [encoder]`
(a playable clip straight from the capture + encode path; `encoder` is auto, hardware
or software, which is how you tell a broken hardware MFT from a broken pipeline).
