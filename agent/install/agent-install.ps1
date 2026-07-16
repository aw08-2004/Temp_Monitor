<#
    Temp_Monitor - C#/.NET Fleet Agent Installer
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
    [string]$InstallDir = "C:\Program Files\TempMonitorAgent",
    [string]$AgentUrl,                       # download URL for the agent exe
    [string]$AgentExe,                       # OR a local path to the agent exe
    [string]$EnrollmentSecret,               # shared secret for POST /api/agent/enroll
    [string]$HubUrl,                          # optional hub base override (TEMP_MONITOR_HUB)
    [string]$CommandSigningPublicKey         # optional 64-hex Ed25519 key (COMMAND_SIGNING_PUBLIC_KEY_HEX)
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$ServiceName = "TempMonitorAgent"
$ExePath     = Join-Path $InstallDir "TempMonitorAgent.exe"
$RegPath     = "HKLM:\SOFTWARE\TempMonitorAgent"
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

  Temp Monitor - Fleet Agent Installer
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
if ($EnrollmentSecret) {
    New-ItemProperty -Path $RegPath -Name "EnrollmentSecret" -Value $EnrollmentSecret -PropertyType String -Force | Out-Null
    Ok "Enrollment secret stored in $RegPath"
} else {
    Warn "No -EnrollmentSecret given; the agent will run telemetry-only until enrolled."
}
if ($HubUrl) {
    [Environment]::SetEnvironmentVariable("TEMP_MONITOR_HUB", $HubUrl, "Machine")
    Ok "Hub override: $HubUrl"
}
if ($CommandSigningPublicKey) {
    [Environment]::SetEnvironmentVariable("COMMAND_SIGNING_PUBLIC_KEY_HEX", $CommandSigningPublicKey, "Machine")
    Ok "Command signing public key configured"
}

# ----------------------------------------------------------------------
# 4. Service (LocalSystem, Automatic) + failure recovery
# ----------------------------------------------------------------------
Step "Registering Windows Service"
if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    Say "Service exists; updating binary path."
    & sc.exe config $ServiceName binPath= "`"$ExePath`"" start= auto | Out-Null
} else {
    New-Service -Name $ServiceName -BinaryPathName "`"$ExePath`"" `
        -DisplayName "TempMonitor Fleet Agent" -StartupType Automatic `
        -Description "Reports telemetry and executes fleet commands for Temp Monitor (RMM)." | Out-Null
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
else { Warn "Service status: $($svc.Status). Check %ProgramData%\TempMonitorAgent\companion.log" }

Write-Host @"

  Done.

  Machine : $env:COMPUTERNAME
  Service : $ServiceName (LocalSystem, Automatic)
  Binary  : $ExePath
  Logs    : $env:ProgramData\TempMonitorAgent\companion.log

  Uninstall: powershell -ExecutionPolicy Bypass -File agent-install.ps1 -Uninstall

"@ -ForegroundColor Green
