# FleetHub — C#/.NET Fleet Agent

A Windows Service (runs under **LocalSystem**) that replaces the Python `companion.py`
for RMM. It reaches telemetry parity with the companion **and** speaks the hub's fleet
command channel: it enrolls, heartbeats, polls for commands, executes them, reports
results, and updates itself from a **signed** manifest (verified fail-closed).

- **Target:** .NET 10 (`net10.0-windows`), published **self-contained single-file
  win-x64** (no runtime install needed on the fleet).
- **Sensors:** LibreHardwareMonitorLib **in-process** — no separate
  LibreHardwareMonitor.exe / `:8085` web server. Needs the **PawnIO** kernel driver
  (the installer sets it up).

## Layout
```
src/TempMonitorAgent/
  Program.cs / Worker.cs         host + main loop
  AgentConfig.cs                 constants, endpoints, trust roots, %ProgramData% paths
  Telemetry/                     SensorReader (LHM), SystemInfo (WMI), TelemetryReporter
  Fleet/                         FleetClient, SignatureVerifier, CommandDispatcher,
                                 Executors/
  Update/                        SelfUpdater, VersionUtil
  State/                         AgentState (agent.json, restart_state.json)
tests/TempMonitorAgent.Tests/    xUnit: update-manifest sig verify, versions
install/agent-install.ps1        installs the service (+ PawnIO, recovery, enroll secret)
```

## Wire protocol (must match the Python hub)
- Telemetry: `POST /api/report` (no auth). Cadence 5s temp / 10s sensors / 600s uptime.
- Fleet: `POST /api/agent/enroll` → `{agent_id, token}`; then
  `Authorization: Bearer <agent_id>:<token>` on `POST /api/agent/heartbeat`,
  `GET /api/agent/commands` (pull+claim), `POST /api/agent/commands/<id>/result`.
- Commands are **not signed**. The agent executes what an enrolled, authenticated pull
  returns; the hub authorizes on an allow-listed console session and records every
  command in its `audit_log`. (Until hub 1.10 / agent 3.1, `run_script`,
  `install_driver` and `update_bios` additionally required an offline Ed25519 signature
  verified here. No key was ever configured, so in practice they were always refused —
  which is why removing the gate is what made them work, not a loosening.)

Implemented executors: `restart`, `shutdown`, `rename`, `gpupdate`, `install_app`,
`run_script`. `install_driver` / `update_bios` are stubs.

**Still signed, and unrelated to the above:** the self-update manifest. `SelfUpdater`
verifies it with `SignatureVerifier.VerifyRaw` against `AgentConfig.UpdatePublicKeyHex`
before any binary replaces the running one. That is what stops a compromised hub from
pushing malicious code to the fleet — do not remove it.

## Configuration
- `TEMP_MONITOR_HUB` — hub base URL (default `https://temp.arkeanos.net`).
- `TEMP_MONITOR_MACHINE` — machine name (default `Environment.MachineName`).
- `AGENT_ENROLLMENT_SECRET` — enrollment secret (installer writes it to
  `HKLM\SOFTWARE\TempMonitorAgent`; env overrides for testing).
- `TEMP_MONITOR_NO_UPDATE=1` — disable self-update (testing).

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
   SCM restarts onto it. **If you skip signing, fleet updates stall** (same rule as
   `companion.py`).

## Install (elevated PowerShell)
```powershell
agent/install/agent-install.ps1 -AgentExe .\dist\TempMonitorAgent.exe -EnrollmentSecret <secret>
agent/install/agent-install.ps1 -AgentUrl <release-url> -EnrollmentSecret <secret> `
    -HubUrl https://temp.arkeanos.net
agent/install/agent-install.ps1 -Uninstall
```
Logs: `%ProgramData%\TempMonitorAgent\companion.log`.
