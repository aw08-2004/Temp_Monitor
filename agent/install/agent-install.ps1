<#
    FleetHub - C#/.NET Fleet Agent Installer
    https://github.com/aw08-2004/Temp_Monitor

    Installs the fleet agent as a Windows Service running under LocalSystem:
      - PawnIO kernel driver (needed by the in-process LibreHardwareMonitorLib for
        sensor access; skipped if already present)
      - The self-contained agent exe -> C:\Program Files\TempMonitorAgent
      - A Windows Service "TempMonitorAgent" (Automatic), with SCM failure-recovery
        set to restart -- this is what the agent's self-update relies on when it
        exits with code 17 to swap onto a new binary.
      - The one-time enrollment secret into HKLM\SOFTWARE\TempMonitorAgent

    Unlike the Python companion (per-logon Scheduled Task in a user session), this
    runs in session 0 as SYSTEM, so restart/rename/gpupdate/scripts work with no one
    logged in.

    Usage (elevated PowerShell):
        powershell -ExecutionPolicy Bypass -File agent-install.ps1 `
            -AgentUrl <release-asset-url> -EnrollmentSecret <secret>
        powershell -ExecutionPolicy Bypass -File agent-install.ps1 -AgentExe .\TempMonitorAgent.exe -EnrollmentSecret <secret>
        powershell -ExecutionPolicy Bypass -File agent-install.ps1 -Uninstall
#>

param(
    [switch]$Uninstall,
    [string]$InstallDir = "C:\Program Files\FleetHub\Agent",
    [string]$AgentUrl,                       # download URL for the agent exe
    [string]$AgentExe,                       # OR a local path to the agent exe
    [string]$EnrollmentSecret,               # shared secret for POST /api/agent/enroll
    [string]$HubUrl                          # optional hub base override (FLEETHUB_HUB)
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# The Windows service name is deliberately NOT renamed yet. .NET sets ServiceBase.ServiceName
# from AddWindowsService() in Program.cs, and a self-updating agent swaps its binary without
# re-registering the service -- so renaming one side without the other leaves the registered
# name and the binary's name disagreeing on exactly the machines already in the field.
# Both move together when the assembly is renamed.
$ServiceName    = "TempMonitorAgent"
$ExeName        = "TempMonitorAgent.exe"
$ExePath        = Join-Path $InstallDir $ExeName
$RegPath        = "HKLM:\SOFTWARE\FleetHub\Agent"
$LegacyRegPath  = "HKLM:\SOFTWARE\TempMonitorAgent"
$LegacyInstall  = "C:\Program Files\TempMonitorAgent"
$PawnIoUrl   = "https://raw.githubusercontent.com/LibreHardwareMonitor/LibreHardwareMonitor/refs/heads/master/LibreHardwareMonitor.Windows.Forms/Resources/PawnIO_setup.exe"

function Say($msg)  { Write-Host "  $msg" }
function Ok($msg)   { Write-Host "  [ok] $msg"   -ForegroundColor Green }
function Warn($msg) { Write-Host "  [!!] $msg"   -ForegroundColor Yellow }
function Die($msg)  { Write-Host "  [xx] $msg"   -ForegroundColor Red; exit 1 }
function Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }

# ----------------------------------------------------------------------
# Elevate
# ----------------------------------------------------------------------
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host "Elevating..." -ForegroundColor Yellow
    if ($PSCommandPath) {
        $argList = @("-ExecutionPolicy","Bypass","-File","`"$PSCommandPath`"")
        foreach ($key in $PSBoundParameters.Keys) {
            $val = $PSBoundParameters[$key]
            if ($val -is [switch]) { if ($val.IsPresent) { $argList += "-$key" } }
            else { $argList += "-$key"; $argList += "`"$val`"" }
        }
        Start-Process powershell -Verb RunAs -ArgumentList $argList
    } else {
        Die "Re-run from a local .ps1 file as administrator."
    }
    exit
}

# ----------------------------------------------------------------------
# Uninstall
# ----------------------------------------------------------------------
if ($Uninstall) {
    Step "Uninstalling $ServiceName"

    if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
        & sc.exe stop $ServiceName | Out-Null
        Start-Sleep -Seconds 2
        & sc.exe delete $ServiceName | Out-Null
        Ok "Removed service $ServiceName"
    } else {
        Say "Service not present."
    }

    if (Test-Path $InstallDir) {
        Remove-Item $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
        Ok "Deleted $InstallDir"
    }

    Warn "Left PawnIO driver and %ProgramData%\TempMonitorAgent (state/logs) in place."
    Write-Host "`nDone.`n" -ForegroundColor Green
    exit
}

Write-Host @"

  FleetHub - Fleet Agent Installer
  Machine: $env:COMPUTERNAME
  Target : $InstallDir

"@ -ForegroundColor Cyan

# ----------------------------------------------------------------------
# 1. PawnIO driver (sensor access for the in-process LibreHardwareMonitorLib)
# ----------------------------------------------------------------------
Step "Installing PawnIO driver"
if (Get-Service -Name "PawnIO" -ErrorAction SilentlyContinue) {
    Ok "Already installed, skipping"
} else {
    $pawnioPath = Join-Path $env:TEMP "PawnIO_setup.exe"
    Say "Downloading $PawnIoUrl"
    Invoke-WebRequest -Uri $PawnIoUrl -OutFile $pawnioPath -UseBasicParsing
    Unblock-File -Path $pawnioPath -ErrorAction SilentlyContinue
    $proc = Start-Process -FilePath $pawnioPath -ArgumentList "-install","-silent" -Wait -PassThru -NoNewWindow
    Remove-Item $pawnioPath -Force -ErrorAction SilentlyContinue
    if ($proc.ExitCode -ne 0) { Warn "PawnIO installer exited $($proc.ExitCode). Sensors may be unreadable." }
    else { Ok "PawnIO installed" }
}

# ----------------------------------------------------------------------
# 2. Agent binary
# ----------------------------------------------------------------------
Step "Installing agent binary"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

# Stop an existing service before overwriting its exe.
if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    & sc.exe stop $ServiceName | Out-Null
    Start-Sleep -Seconds 2
}

# Pre-rename installs live at C:\Program Files\TempMonitorAgent. Move them under the
# shared FleetHub root so hub and agent sit together, and point the existing service
# registration at the new path (below) rather than leaving a second copy behind.
# %ProgramData% state and the enrollment secret are handled by the agent itself.
if ($InstallDir -ne $LegacyInstall -and (Test-Path $LegacyInstall)) {
    Say "Migrating existing install from $LegacyInstall"
    foreach ($stale in @("$ExeName.old")) {
        Remove-Item (Join-Path $LegacyInstall $stale) -Force -ErrorAction SilentlyContinue
    }
    Get-ChildItem -File $LegacyInstall -ErrorAction SilentlyContinue | ForEach-Object {
        Move-Item $_.FullName (Join-Path $InstallDir $_.Name) -Force -ErrorAction SilentlyContinue
    }
    Remove-Item $LegacyInstall -Recurse -Force -ErrorAction SilentlyContinue
    Ok "Moved agent to $InstallDir"
}

if ($AgentExe) {
    if (-not (Test-Path $AgentExe)) { Die "AgentExe not found: $AgentExe" }
    Copy-Item -Path $AgentExe -Destination $ExePath -Force
    Ok "Copied $AgentExe -> $ExePath"
} elseif ($AgentUrl) {
    Say "Downloading $AgentUrl"
    Invoke-WebRequest -Uri $AgentUrl -OutFile $ExePath -UseBasicParsing
    Unblock-File -Path $ExePath -ErrorAction SilentlyContinue
    Ok "Downloaded -> $ExePath"
} else {
    Die "Provide -AgentUrl <release-asset-url> or -AgentExe <local exe path>."
}

# ----------------------------------------------------------------------
# 3. Configuration (enrollment secret + optional overrides)
# ----------------------------------------------------------------------
Step "Writing configuration"
New-Item -Path $RegPath -Force | Out-Null

# Carry a secret written by a pre-rename installer forward, so re-running this on an
# enrolled machine without -EnrollmentSecret doesn't silently drop it back to
# telemetry-only. The agent reads both keys, but consolidating here means the legacy
# key can eventually be retired.
$secret = $EnrollmentSecret
if (-not $secret) {
    $secret = (Get-ItemProperty -Path $LegacyRegPath -Name "EnrollmentSecret" -ErrorAction SilentlyContinue).EnrollmentSecret
    if ($secret) { Say "Reusing the enrollment secret from $LegacyRegPath" }
}
if ($secret) {
    New-ItemProperty -Path $RegPath -Name "EnrollmentSecret" -Value $secret -PropertyType String -Force | Out-Null
    Ok "Enrollment secret stored in $RegPath"
} else {
    Warn "No -EnrollmentSecret given; the agent will run telemetry-only until enrolled."
}

if ($HubUrl) {
    [Environment]::SetEnvironmentVariable("FLEETHUB_HUB", $HubUrl, "Machine")
    # Clear the pre-rename variable so the two can't drift apart and leave an operator
    # wondering which one the agent is honouring (it prefers FLEETHUB_HUB).
    if ([Environment]::GetEnvironmentVariable("TEMP_MONITOR_HUB", "Machine")) {
        [Environment]::SetEnvironmentVariable("TEMP_MONITOR_HUB", $null, "Machine")
    }
    Ok "Hub override: $HubUrl"
}
# Commands are no longer signed, so this machine-level key is dead config. Clear a
# stale one left by a pre-1.10 install rather than leaving it to confuse the next
# person who greps the environment for it.
if ([Environment]::GetEnvironmentVariable("COMMAND_SIGNING_PUBLIC_KEY_HEX", "Machine")) {
    [Environment]::SetEnvironmentVariable("COMMAND_SIGNING_PUBLIC_KEY_HEX", $null, "Machine")
    Ok "Removed obsolete COMMAND_SIGNING_PUBLIC_KEY_HEX (commands are no longer signed)"
}

# ----------------------------------------------------------------------
# 4. Service (LocalSystem, Automatic) + failure recovery
# ----------------------------------------------------------------------
Step "Registering Windows Service"
if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    Say "Service exists; updating binary path."
    # binPath must be re-pointed after a migration moved the exe under the shared root.
    & sc.exe config $ServiceName binPath= "`"$ExePath`"" start= auto DisplayName= "FleetHub Agent" | Out-Null
} else {
    New-Service -Name $ServiceName -BinaryPathName "`"$ExePath`"" `
        -DisplayName "FleetHub Agent" -StartupType Automatic `
        -Description "Reports telemetry and executes fleet commands for FleetHub (RMM)." | Out-Null
    Ok "Created service $ServiceName"
}

# Restart on failure -- the self-update exits with code 17 and relies on this.
& sc.exe failure $ServiceName reset= 86400 actions= restart/60000/restart/60000/restart/60000 | Out-Null
Ok "Failure recovery: restart x3 @ 60s"

# ----------------------------------------------------------------------
# 5. Start
# ----------------------------------------------------------------------
Step "Starting service"
& sc.exe start $ServiceName | Out-Null
Start-Sleep -Seconds 2
$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -eq "Running") { Ok "Service running" }
else { Warn "Service status: $($svc.Status). Check %ProgramData%\FleetHub\Agent\companion.log" }

Write-Host @"

  Done.

  Machine : $env:COMPUTERNAME
  Service : $ServiceName (LocalSystem, Automatic)
  Binary  : $ExePath
  Logs    : $env:ProgramData\FleetHub\Agent\companion.log

  Uninstall: powershell -ExecutionPolicy Bypass -File agent-install.ps1 -Uninstall

"@ -ForegroundColor Green
