<#
    Temp Monitor - Unified Installer
    https://github.com/aw08-2004/Temp_Monitor

    Interactive menu over the three install paths:
      1) Agent      - C#/.NET Windows Service (recommended for new machines)
      2) Companion  - legacy Python scheduled-task agent
      3) Hub        - Flask/Socket.IO server (this machine becomes the fleet hub)

    Prompts for whatever each path needs (enrollment secret, hub URL, OAuth
    creds, etc.), defaulting to values already present in a local .env when run
    from a clone. Non-interactive use is still supported by passing -Component
    plus the relevant parameters up front.

    Usage (elevated PowerShell):
        powershell -ExecutionPolicy Bypass -File install.ps1
        powershell -ExecutionPolicy Bypass -File install.ps1 -Component Agent -AgentUrl <url> -EnrollmentSecret <secret>
        powershell -ExecutionPolicy Bypass -File install.ps1 -Component Companion
        powershell -ExecutionPolicy Bypass -File install.ps1 -Component Hub
        powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall                    # legacy companion (back-compat)
        powershell -ExecutionPolicy Bypass -File install.ps1 -Component Agent -Uninstall
        powershell -ExecutionPolicy Bypass -File install.ps1 -Component Hub -Uninstall

    From the web:
        irm https://raw.githubusercontent.com/aw08-2004/Temp_Monitor/main/install.ps1 | iex
#>

param(
    [ValidateSet("Agent", "Companion", "Hub")]
    [string]$Component,
    [switch]$Uninstall,

    # --- Companion (legacy) ---
    [string]$InstallDir = "C:\Program Files\TempMonitor",
    [int]$Port = 8085,

    # --- Agent ---
    [string]$AgentUrl,
    [string]$AgentExe,
    [string]$EnrollmentSecret,
    [string]$HubUrl,

    # --- Hub ---
    [int]$HubPort = 3001,
    [string]$HubInstallDir
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Repo           = "aw08-2004/Temp_Monitor"
$InstallerUrl   = "https://raw.githubusercontent.com/$Repo/main/install.ps1"
$CompanionUrl   = "https://raw.githubusercontent.com/$Repo/main/companion.py"
$AgentInstallUrl= "https://raw.githubusercontent.com/$Repo/main/agent/install/agent-install.ps1"
$LhmApi         = "https://api.github.com/repos/LibreHardwareMonitor/LibreHardwareMonitor/releases/latest"
$LhmFallback    = "https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/download/v0.9.6/LibreHardwareMonitor.zip"
$PythonFallback = "https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe"
$PawnIoUrl      = "https://raw.githubusercontent.com/LibreHardwareMonitor/LibreHardwareMonitor/refs/heads/master/LibreHardwareMonitor.Windows.Forms/Resources/PawnIO_setup.exe"
$LhmDir         = Join-Path $InstallDir "LibreHardwareMonitor"
$TaskLhm        = "TempMonitor - LibreHardwareMonitor"
$TaskCompanion  = "TempMonitor - Companion"
$TaskHub        = "TempMonitor - Hub"          # legacy scheduled task (pre-service), cleaned up on install/uninstall

# --- Hub-as-Windows-Service ---
$HubInstallDefault = "C:\Program Files\TempMonitor\Hub"
$RepoGitUrl        = "https://github.com/$Repo.git"
# WinSW wraps the Python/waitress process as a real Windows Service (Python can't be one on
# its own). Pinned to a stable v2 release; same "download a pinned asset" pattern as LHM/PawnIO.
$WinSwUrl          = "https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe"
$HubServiceId      = "TempMonitorHub"
$HubServiceName    = "Temp Monitor - Hub"

function Say($msg)  { Write-Host "  $msg" }
function Ok($msg)   { Write-Host "  [ok] $msg"   -ForegroundColor Green }
function Warn($msg) { Write-Host "  [!!] $msg"   -ForegroundColor Yellow }
function Die($msg)  { Write-Host "  [xx] $msg"   -ForegroundColor Red; exit 1 }
function Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }

# ----------------------------------------------------------------------
# Small helpers shared by every install path
# ----------------------------------------------------------------------
function Mask([string]$v) {
    if (-not $v) { return "" }
    if ($v.Length -le 6) { return "******" }
    return $v.Substring(0, 3) + ("*" * 6) + $v.Substring($v.Length - 3)
}

function Prompt-Value([string]$Label, [string]$Default = "", [switch]$Secret) {
    $shown = ""
    if ($Default) { if ($Secret) { $shown = " [" + (Mask $Default) + "]" } else { $shown = " [$Default]" } }
    $val = Read-Host "$Label$shown"
    $val = "$val".Trim()
    if (-not $val) { return $Default }
    return $val
}

function New-RandomSecret([int]$Bytes = 24) {
    $b = New-Object byte[] $Bytes
    [Security.Cryptography.RandomNumberGenerator]::Fill($b)
    -join ($b | ForEach-Object { $_.ToString("x2") })
}

function Read-DotEnv([string]$Path) {
    $result = @{}
    if (Test-Path $Path) {
        Get-Content $Path | ForEach-Object {
            if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') {
                $result[$matches[1]] = $matches[2]
            }
        }
    }
    return $result
}

function Resolve-Python {
    foreach ($cmd in @("py -3", "python")) {
        $exe, $args = $cmd -split " ", 2
        if (Get-Command $exe -ErrorAction SilentlyContinue) {
            try {
                $v = & $exe $args --version 2>&1
                if ($v -match "Python 3") { return @{ Exe = (Get-Command $exe).Source; Args = $args; Version = "$v" } }
            } catch { }
        }
    }
    return $null
}

function Get-LatestAgentAssetUrl {
    try {
        $rels = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases" -Headers @{ "User-Agent" = "TempMonitor-Installer" } -TimeoutSec 15
        $rel = $rels | Where-Object { $_.tag_name -like "agent-v*" } | Select-Object -First 1
        if ($rel) {
            $asset = $rel.assets | Where-Object { $_.name -eq "TempMonitorAgent.exe" } | Select-Object -First 1
            if ($asset) { return $asset.browser_download_url }
        }
    } catch { }
    return $null
}

function Show-Menu {
    Write-Host @"

  Temp Monitor - Unified Installer
  =================================
   1) Install Agent      (C#/.NET Windows Service - recommended)
   2) Install Companion  (legacy Python scheduled-task agent)
   3) Install Hub        (Flask/Socket.IO server - this machine becomes the fleet hub)
   4) Uninstall...
   0) Exit

"@ -ForegroundColor Cyan
    $choice = Read-Host "Choose an option"
    switch ($choice) {
        "1" { return "Agent" }
        "2" { return "Companion" }
        "3" { return "Hub" }
        "4" { return "UninstallMenu" }
        "0" { exit }
        default { Warn "Invalid choice."; return Show-Menu }
    }
}

function Show-UninstallMenu {
    Write-Host "`n  Which component do you want to uninstall?" -ForegroundColor Cyan
    Write-Host "   1) Agent"
    Write-Host "   2) Companion"
    Write-Host "   3) Hub"
    Write-Host "   0) Cancel"
    $choice = Read-Host "Choose an option"
    switch ($choice) {
        "1" { return "Agent" }
        "2" { return "Companion" }
        "3" { return "Hub" }
        "0" { exit }
        default { Warn "Invalid choice."; return Show-UninstallMenu }
    }
}

# ----------------------------------------------------------------------
# Elevate: every path below needs admin (LHM/service/task registration)
# ----------------------------------------------------------------------
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host "Elevating..." -ForegroundColor Yellow

    $remoteArgList = foreach ($key in $PSBoundParameters.Keys) {
        $val = $PSBoundParameters[$key]
        if ($val -is [switch]) { if ($val.IsPresent) { "-$key" } }
        else { "-$key"; "`"$val`"" }
    }

    if ($PSCommandPath) {
        # Running from a local file -- relaunch that same file, forwarding every
        # bound parameter (not just -Uninstall) so -Component/-AgentUrl/etc. survive.
        $argList = @("-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"") + $remoteArgList
        Start-Process powershell -Verb RunAs -ArgumentList $argList
    } else {
        # Running via `irm | iex` -- no script file to relaunch, so re-fetch
        # and re-invoke as a scriptblock (preserves param binding) in the
        # elevated process.
        $cmd = "& ([scriptblock]::Create((irm '$InstallerUrl'))) $($remoteArgList -join ' ')"
        Start-Process powershell -Verb RunAs -ArgumentList @("-ExecutionPolicy", "Bypass", "-Command", $cmd)
    }
    exit
}

# ========================================================================
# Companion (legacy Python scheduled-task agent)
# ========================================================================
function Uninstall-Companion {
    Step "Uninstalling Temp Monitor Companion"

    foreach ($t in @($TaskCompanion, $TaskLhm)) {
        if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $t -Confirm:$false
            Ok "Removed task: $t"
        }
    }

    Get-Process -Name "LibreHardwareMonitor" -ErrorAction SilentlyContinue | Stop-Process -Force
    Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
        Where-Object { $_.CommandLine -like "*companion.py*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Ok "Stopped running processes"

    if (Test-Path $InstallDir) {
        Remove-Item $InstallDir -Recurse -Force
        Ok "Deleted $InstallDir"
    }

    Write-Host "`nDone. Python itself was left alone.`n" -ForegroundColor Green
}

function Install-Companion {
    Write-Host @"

  Temp Monitor - Companion Agent Installer
  Machine: $env:COMPUTERNAME
  Target : $InstallDir

"@ -ForegroundColor Cyan

    # ------------------------------------------------------------------
    # 1. Python
    # ------------------------------------------------------------------
    Step "Checking Python"

    $py = Resolve-Python
    if (-not $py) {
        Warn "Python not found."

        if (Get-Command winget -ErrorAction SilentlyContinue) {
            Say "Installing via winget..."
            # Pin --source winget: without it, winget also probes the msstore
            # source, and a bad msstore cert/network on the machine aborts the
            # whole install even though the winget source works fine.
            winget install --id Python.Python.3.12 --source winget --scope machine `
                --silent --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -ne 0) { Warn "winget install failed (exit $LASTEXITCODE)." }

            $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                        [Environment]::GetEnvironmentVariable("Path", "User")
            $py = Resolve-Python
        } else {
            Warn "winget is unavailable."
        }

        if (-not $py) {
            Say "Falling back to direct download from python.org..."
            $pyInstaller = Join-Path $env:TEMP "python-installer.exe"
            try {
                Invoke-WebRequest -Uri $PythonFallback -OutFile $pyInstaller -UseBasicParsing
                $proc = Start-Process -FilePath $pyInstaller -Wait -PassThru -ArgumentList `
                    "/quiet", "InstallAllUsers=1", "PrependPath=1", "Include_test=0"
                if ($proc.ExitCode -ne 0) { Warn "python.org installer exited with code $($proc.ExitCode)." }
            } catch {
                Warn "Direct download failed: $_"
            } finally {
                Remove-Item $pyInstaller -Force -ErrorAction SilentlyContinue
            }

            $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                        [Environment]::GetEnvironmentVariable("Path", "User")
            $py = Resolve-Python
        }

        if (-not $py) {
            Die "Python still not on PATH. Reboot and re-run the installer, or install Python 3 manually from python.org (tick 'Add to PATH')."
        }
    }
    Ok "Found $($py.Version)"

    # Resolve the real interpreter path (so scheduled tasks don't depend on PATH)
    $pythonExe = & $py.Exe $py.Args -c "import sys; print(sys.executable)"
    $pythonwExe = Join-Path (Split-Path $pythonExe) "pythonw.exe"   # windowless, no console popup
    if (-not (Test-Path $pythonwExe)) { $pythonwExe = $pythonExe }
    Ok "Interpreter: $pythonExe"

    Step "Installing Python packages"
    & $pythonExe -m pip install --upgrade pip --quiet
    # cryptography is needed so the companion can verify signed self-updates (Ed25519).
    & $pythonExe -m pip install requests cryptography --quiet
    if ($LASTEXITCODE -ne 0) { Die "pip install failed." }
    Ok "requests + cryptography installed"

    # ------------------------------------------------------------------
    # 2. Files
    # ------------------------------------------------------------------
    Step "Setting up $InstallDir"
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    New-Item -ItemType Directory -Force -Path $LhmDir     | Out-Null
    Ok "Directories ready"

    # ------------------------------------------------------------------
    # 3. LibreHardwareMonitor
    # ------------------------------------------------------------------
    Step "Installing LibreHardwareMonitor"

    $lhmExe = Join-Path $LhmDir "LibreHardwareMonitor.exe"

    if (Test-Path $lhmExe) {
        Ok "Already present, skipping download"
    } else {
        $zipUrl = $LhmFallback
        try {
            $rel = Invoke-RestMethod -Uri $LhmApi -Headers @{ "User-Agent" = "TempMonitor-Installer" } -TimeoutSec 15
            $asset = $rel.assets | Where-Object { $_.name -like "*net472*.zip" } | Select-Object -First 1
            if ($asset) {
                $zipUrl = $asset.browser_download_url
                Say "Latest release: $($rel.tag_name)"
            }
        } catch {
            Warn "GitHub API unreachable (rate limit?). Using pinned v0.9.6."
        }

        $zipPath = Join-Path $env:TEMP "LibreHardwareMonitor.zip"
        Say "Downloading $zipUrl"
        Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath -UseBasicParsing
        Expand-Archive -Path $zipPath -DestinationPath $LhmDir -Force
        Remove-Item $zipPath -Force

        # Some releases nest everything one folder deep
        if (-not (Test-Path $lhmExe)) {
            $found = Get-ChildItem $LhmDir -Recurse -Filter "LibreHardwareMonitor.exe" | Select-Object -First 1
            if ($found) {
                Get-ChildItem $found.DirectoryName | Move-Item -Destination $LhmDir -Force
            }
        }
        if (-not (Test-Path $lhmExe)) { Die "LibreHardwareMonitor.exe not found after extraction." }

        Unblock-File -Path (Join-Path $LhmDir "*") -ErrorAction SilentlyContinue
        Ok "Extracted to $LhmDir"
    }

    # ------------------------------------------------------------------
    # 4. PawnIO -- kernel driver LHM needs for sensor access (replaces WinRing0)
    # ------------------------------------------------------------------
    Step "Installing PawnIO driver"

    if (Get-Service -Name "PawnIO" -ErrorAction SilentlyContinue) {
        Ok "Already installed, skipping"
    } else {
        $pawnioPath = Join-Path $env:TEMP "PawnIO_setup.exe"
        Say "Downloading $PawnIoUrl"
        Invoke-WebRequest -Uri $PawnIoUrl -OutFile $pawnioPath -UseBasicParsing
        Unblock-File -Path $pawnioPath -ErrorAction SilentlyContinue

        $proc = Start-Process -FilePath $pawnioPath -ArgumentList "-install", "-silent" -Wait -PassThru -NoNewWindow
        Remove-Item $pawnioPath -Force -ErrorAction SilentlyContinue

        if ($proc.ExitCode -ne 0) {
            Warn "PawnIO installer exited with code $($proc.ExitCode). Sensors may not be readable."
        } else {
            Ok "PawnIO installed"
        }
    }

    # ------------------------------------------------------------------
    # 5. LHM config -- web server ON, start minimized, live in the tray
    #    LHM reads <exe name>.config from its own folder (PersistentSettings)
    # ------------------------------------------------------------------
    Step "Configuring LibreHardwareMonitor web server (port $Port)"

    $lhmConfig = Join-Path $LhmDir "LibreHardwareMonitor.config"
    @"
<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <appSettings>
    <add key="runWebServerMenuItem" value="true" />
    <add key="listenerPort" value="$Port" />
    <add key="authenticationEnabled" value="false" />
    <add key="startMinMenuItem" value="true" />
    <add key="minTrayMenuItem" value="true" />
    <add key="minCloseMenuItem" value="true" />
    <add key="cpuMenuItem" value="true" />
    <add key="mainForm.Location.X" value="100" />
    <add key="mainForm.Location.Y" value="100" />
  </appSettings>
</configuration>
"@ | Set-Content -Path $lhmConfig -Encoding UTF8

    Ok "Wrote $lhmConfig"

    # ------------------------------------------------------------------
    # 6. companion.py
    # ------------------------------------------------------------------
    Step "Downloading companion.py"
    $companionPath = Join-Path $InstallDir "companion.py"
    Invoke-WebRequest -Uri $CompanionUrl -OutFile $companionPath -UseBasicParsing
    $ver = (Select-String -Path $companionPath -Pattern '^VERSION\s*=\s*"([\d.]+)"').Matches.Groups[1].Value
    Ok "companion.py v$ver -> $companionPath"

    # ------------------------------------------------------------------
    # 7. Scheduled tasks (RunLevel Highest = admin without a UAC prompt every logon)
    # ------------------------------------------------------------------
    Step "Registering scheduled tasks"

    # Pass the SID directly rather than a "DOMAIN\User" string -- some machines
    # (seen on ones with a leftover/corrupted HomeGroup profile) fail the internal
    # name-to-SID lookup Register-ScheduledTask does for a name string, with
    # "No mapping between account names and security IDs was done" (0x80070534).
    # The SID is already resolved, so it skips that lookup entirely.
    $currentUserSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $principal = New-ScheduledTaskPrincipal -UserId $currentUserSid `
                                            -LogonType Interactive -RunLevel Highest
    $settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                                              -DontStopIfGoingOnBatteries `
                                              -StartWhenAvailable `
                                              -ExecutionTimeLimit ([TimeSpan]::Zero) `
                                              -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

    # LHM first
    Register-ScheduledTask -TaskName $TaskLhm -Force `
        -Action    (New-ScheduledTaskAction -Execute $lhmExe -WorkingDirectory $LhmDir) `
        -Trigger   (New-ScheduledTaskTrigger -AtLogOn) `
        -Principal $principal -Settings $settings `
        -Description "Hardware sensor daemon for Temp Monitor. Serves JSON on localhost:$Port." | Out-Null
    Ok "Task: $TaskLhm"

    # Companion 30s later, so LHM's web server is up. Also repeats every 2 minutes
    # (indefinitely) as a self-heal mechanism: Task Scheduler puts the task in a job
    # object that kills any child we spawn when we exit, so neither a "detached"
    # relaunch helper nor the -RestartCount/-RestartInterval settings above reliably
    # bring the task back after companion.py swaps itself during a self-update (verified
    # empirically -- RestartCount/RestartInterval do not fire on a plain nonzero exit,
    # they're for a narrower "task failed to launch" class). The repetition trigger is
    # the one relaunch path that's actually reliable, since it's driven by the Task
    # Scheduler service itself, not a descendant of our job. -MultipleInstances
    # IgnoreNew (the default) means a tick while we're already running is a no-op; it
    # only actually starts a new instance once we've exited.
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $trigger.Delay = "PT30S"
    $trigger.Repetition.Interval = "PT2M"
    # Duration deliberately left empty: Task Scheduler rejects year/month designators
    # there (e.g. "P10Y" errors as "incorrectly formatted or out of range"), and an
    # empty Duration already means "repeat every Interval indefinitely".
    Register-ScheduledTask -TaskName $TaskCompanion -Force `
        -Action    (New-ScheduledTaskAction -Execute $pythonwExe -Argument "`"$companionPath`"" -WorkingDirectory $InstallDir) `
        -Trigger   $trigger `
        -Principal $principal -Settings $settings `
        -Description "Reports CPU temperature to the Temp Monitor hub." | Out-Null
    Ok "Task: $TaskCompanion (30s delay, repeats every 2min as a self-heal restart path)"

    # ------------------------------------------------------------------
    # 8. Start and verify
    # ------------------------------------------------------------------
    Step "Starting services"

    if (-not (Get-Process -Name "LibreHardwareMonitor" -ErrorAction SilentlyContinue)) {
        Start-ScheduledTask -TaskName $TaskLhm
    }

    Say "Waiting for the sensor web server..."
    $live = $false
    foreach ($i in 1..20) {
        Start-Sleep -Seconds 1
        try {
            $r = Invoke-RestMethod -Uri "http://localhost:$Port/data.json" -TimeoutSec 2
            $live = $true
            break
        } catch { }
    }

    if (-not $live) {
        Warn "No response on port $Port after 20s."
        Warn "Open $lhmExe manually and check Options > Run web server."
    } else {
        Ok "Web server responding on http://localhost:$Port/data.json"

        # Show what the companion will actually pick up
        function Find-Temps($node, $inCpu) {
            if ("$($node.HardwareId)" -like "*cpu*") { $inCpu = $true }
            if ($inCpu -and $node.Type -eq "Temperature") {
                $script:temps += [pscustomobject]@{ Sensor = $node.Text; Value = $node.Value }
            }
            foreach ($c in $node.Children) { Find-Temps $c $inCpu }
        }
        $script:temps = @()
        Find-Temps $r $false
        if ($script:temps.Count -gt 0) {
            Say "CPU sensors detected:"
            $script:temps | ForEach-Object { Say "   $($_.Sensor): $($_.Value)" }
        } else {
            Warn "No CPU temperature sensors visible. LHM may need a reboot to load its kernel driver."
        }

        Start-ScheduledTask -TaskName $TaskCompanion
        Ok "Companion started"
    }

    Write-Host @"

  Done.

  Machine name reported to the hub: $env:COMPUTERNAME
  Sensors : http://localhost:$Port/data.json
  Files   : $InstallDir

  companion.py updates itself from GitHub on every start, and weekly if left running.
  Uninstall: powershell -ExecutionPolicy Bypass -File install.ps1 -Component Companion -Uninstall

"@ -ForegroundColor Green
}

# ========================================================================
# Agent (C#/.NET Windows Service)
# ========================================================================
function Uninstall-Agent {
    Step "Uninstalling Agent"
    $localInstaller = $null
    if ($PSScriptRoot) {
        $p = Join-Path $PSScriptRoot "agent\install\agent-install.ps1"
        if (Test-Path $p) { $localInstaller = $p }
    }

    if ($localInstaller) {
        & $localInstaller -Uninstall
    } else {
        $tmp = Join-Path $env:TEMP "temp-monitor-agent-install.ps1"
        Invoke-WebRequest -Uri $AgentInstallUrl -OutFile $tmp -UseBasicParsing
        & $tmp -Uninstall
    }
}

function Install-Agent {
    Write-Host @"

  Temp Monitor - Agent Installer (C#/.NET Windows Service)

"@ -ForegroundColor Cyan

    $envDefaults = @{}
    if ($PSScriptRoot) { $envDefaults = Read-DotEnv (Join-Path $PSScriptRoot ".env") }

    $resolvedExe = $AgentExe
    $resolvedUrl = $AgentUrl

    if (-not $resolvedExe -and -not $resolvedUrl) {
        $localExeDefault = $null
        if ($PSScriptRoot) {
            $p = Join-Path $PSScriptRoot "agent\dist\TempMonitorAgent.exe"
            if (Test-Path $p) { $localExeDefault = $p }
        }

        if ($localExeDefault) {
            $useLocal = Read-Host "Found a built exe at $localExeDefault. Use it? (Y/n)"
            if ($useLocal -notmatch '^[Nn]') { $resolvedExe = $localExeDefault }
        }

        if (-not $resolvedExe) {
            Say "Looking up the latest agent release on GitHub..."
            $latest = Get-LatestAgentAssetUrl
            $resolvedUrl = Prompt-Value "Agent download URL" $latest
            if (-not $resolvedUrl) { Die "No agent URL available. Re-run with -AgentUrl <url> or -AgentExe <path>." }
        }
    }

    $hubUrlDefault = $envDefaults["HUB_URL"]
    if (-not $hubUrlDefault) { $hubUrlDefault = "https://temp.arkeanos.net" }
    if ($HubUrl) { $hubUrlDefault = $HubUrl }
    $resolvedHubUrl = Prompt-Value "Hub URL" $hubUrlDefault

    $secretDefault = $envDefaults["AGENT_ENROLLMENT_SECRET"]
    if ($EnrollmentSecret) { $secretDefault = $EnrollmentSecret }
    $resolvedSecret = Prompt-Value "Agent enrollment secret (blank = telemetry-only until enrolled later)" $secretDefault -Secret

    $agentArgs = @{}
    if ($resolvedExe) { $agentArgs.AgentExe = $resolvedExe } else { $agentArgs.AgentUrl = $resolvedUrl }
    if ($resolvedSecret) { $agentArgs.EnrollmentSecret = $resolvedSecret }
    if ($resolvedHubUrl) { $agentArgs.HubUrl = $resolvedHubUrl }

    $localInstaller = $null
    if ($PSScriptRoot) {
        $p = Join-Path $PSScriptRoot "agent\install\agent-install.ps1"
        if (Test-Path $p) { $localInstaller = $p }
    }

    if ($localInstaller) {
        & $localInstaller @agentArgs
    } else {
        $tmp = Join-Path $env:TEMP "temp-monitor-agent-install.ps1"
        Invoke-WebRequest -Uri $AgentInstallUrl -OutFile $tmp -UseBasicParsing
        & $tmp @agentArgs
    }
}

# ========================================================================
# Hub (Flask + Socket.IO)
# ========================================================================
# Where the hub lives: -HubInstallDir if given, else the default under Program Files, else --
# for uninstall -- an existing clone next to install.ps1. Kept in one place so install and
# uninstall agree on the location.
function Resolve-HubDir {
    if ($HubInstallDir) { return $HubInstallDir.TrimEnd('\') }
    if (Test-Path (Join-Path $HubInstallDefault "app.py")) { return $HubInstallDefault }
    if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot "app.py"))) { return $PSScriptRoot }
    return $HubInstallDefault
}

function Remove-HubService([string]$hubDir) {
    if (-not (Get-Service -Name $HubServiceId -ErrorAction SilentlyContinue)) { return $false }
    $wrapperExe = Join-Path $hubDir "$HubServiceId.exe"
    if (Test-Path $wrapperExe) {
        & $wrapperExe stop      2>&1 | Out-Null
        & $wrapperExe uninstall 2>&1 | Out-Null
    } else {
        # Wrapper exe is gone but the service registration lingers -- tear it down directly.
        & sc.exe stop   $HubServiceId | Out-Null
        & sc.exe delete $HubServiceId | Out-Null
    }
    Start-Sleep -Seconds 2
    return $true
}

function Uninstall-Hub {
    Step "Uninstalling Hub"
    $hubDir = Resolve-HubDir
    if (Remove-HubService $hubDir) {
        Ok "Removed service: $HubServiceName"
    } else {
        Say "Service not present."
    }
    # Also clear the legacy scheduled task, in case this box predates the service.
    if (Get-ScheduledTask -TaskName $TaskHub -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskHub -Confirm:$false
        Ok "Removed legacy scheduled task: $TaskHub"
    }
    Warn "Left the hub files at $hubDir (including .env and logs) in place -- they may still hold data you want."
    Write-Host "`nDone.`n" -ForegroundColor Green
}

function Install-Hub {
    # Resolve where the hub will live and make sure it's a git clone (self-update does
    # `git reset --hard origin/main`, so the tree needs a GitHub origin). If the target
    # already has app.py it's an existing clone (or the folder we were launched from);
    # otherwise clone into it.
    $default = if ($HubInstallDir) { $HubInstallDir } else { $HubInstallDefault }
    $hubDir  = (Prompt-Value "Hub install location" $default).TrimEnd('\')

    Write-Host @"

  Temp Monitor - Hub Installer
  Installs app.py (Flask + Socket.IO) as the '$HubServiceName' Windows Service.
  Location: $hubDir

"@ -ForegroundColor Cyan

    Step "Preparing hub files at $hubDir"
    if (Test-Path (Join-Path $hubDir "app.py")) {
        Ok "Using existing repo at $hubDir"
        if (-not (Test-Path (Join-Path $hubDir ".git"))) {
            Warn "$hubDir is not a git clone -- hub self-update (HUB_AUTO_UPDATE) will not work here."
        }
    } else {
        if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
            Die "git is required to install the hub to a new location (and for hub self-update). Install Git for Windows, then re-run."
        }
        if ((Test-Path $hubDir) -and (Get-ChildItem -Force $hubDir | Select-Object -First 1)) {
            Die "$hubDir already exists, is not a repo clone (no app.py), and is not empty. Pick an empty/new folder or an existing clone."
        }
        $parent = Split-Path $hubDir -Parent
        if ($parent -and -not (Test-Path $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
        Say "Cloning $RepoGitUrl ..."
        & git clone $RepoGitUrl "$hubDir"
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path (Join-Path $hubDir "app.py"))) { Die "git clone failed." }
        Ok "Cloned hub to $hubDir"
    }

    $envPath  = Join-Path $hubDir ".env"
    $existing = Read-DotEnv $envPath

    Step "Checking Python"
    $py = Resolve-Python
    if (-not $py) { Die "Python 3 not found. Install Python 3 first (python.org, tick 'Add to PATH'), then re-run." }
    $pythonExe = & $py.Exe $py.Args -c "import sys; print(sys.executable)"
    Ok "Interpreter: $pythonExe"

    Step "Installing Python packages"
    & $pythonExe -m pip install --upgrade pip --quiet
    & $pythonExe -m pip install -r (Join-Path $hubDir "requirements.txt") --quiet
    if ($LASTEXITCODE -ne 0) { Die "pip install failed." }
    Ok "Dependencies installed"

    Step "Configuring .env"
    Say "Press Enter to keep the value shown in [brackets]."
    $googleId      = Prompt-Value "Google OAuth client ID" $existing["GOOGLE_CLIENT_ID"]
    $googleSecret  = Prompt-Value "Google OAuth client secret" $existing["GOOGLE_CLIENT_SECRET"] -Secret

    $flaskSecretDefault = $existing["FLASK_SECRET_KEY"]
    if (-not $flaskSecretDefault) { $flaskSecretDefault = New-RandomSecret }
    $flaskSecret   = Prompt-Value "Flask session secret key" $flaskSecretDefault -Secret

    $allowedEmails = Prompt-Value "Allowed Google emails (comma-separated)" $existing["ALLOWED_EMAILS"]

    $hubUrlDefault = $existing["HUB_URL"]
    if (-not $hubUrlDefault) { $hubUrlDefault = "https://temp.arkeanos.net" }
    $hubUrlValue   = Prompt-Value "Public hub URL" $hubUrlDefault

    Say ""
    Say "Fleet command channel (optional -- leave blank to keep telemetry-only):"
    $enrollSecretDefault = $existing["AGENT_ENROLLMENT_SECRET"]
    if (-not $enrollSecretDefault) {
        $gen = Read-Host "  No AGENT_ENROLLMENT_SECRET set. Auto-generate one? (Y/n)"
        if ($gen -notmatch '^[Nn]') { $enrollSecretDefault = New-RandomSecret }
    }
    $enrollSecret  = Prompt-Value "  Agent enrollment secret" $enrollSecretDefault -Secret

    Say ""
    $autoUpdateDefault = $existing["HUB_AUTO_UPDATE"]
    if (-not $autoUpdateDefault) {
        $au = Read-Host "  Enable hub self-update from main (git reset --hard)? (y/N)"
        if ($au -match '^[Yy]') { $autoUpdateDefault = "1" }
    }

    $lines = @(
        "GOOGLE_CLIENT_ID=$googleId"
        "GOOGLE_CLIENT_SECRET=$googleSecret"
        "FLASK_SECRET_KEY=$flaskSecret"
        "ALLOWED_EMAILS=$allowedEmails"
        "HUB_URL=$hubUrlValue"
    )
    if ($enrollSecret)      { $lines += "AGENT_ENROLLMENT_SECRET=$enrollSecret" }
    if ($autoUpdateDefault) { $lines += "HUB_AUTO_UPDATE=$autoUpdateDefault" }
    Set-Content -Path $envPath -Value $lines -Encoding UTF8
    Ok "Wrote $envPath"

    Step "Installing the $HubServiceName service"
    # Serve via waitress; prefer its console script, fall back to `python -m waitress`.
    $scriptsDir  = Join-Path (Split-Path $pythonExe -Parent) "Scripts"
    $waitressExe = Join-Path $scriptsDir "waitress-serve.exe"
    if (Test-Path $waitressExe) {
        $hubExec = $waitressExe
        $hubArgs = "--host=0.0.0.0 --port=$HubPort wsgi:application"
    } else {
        $hubExec = $pythonExe
        $hubArgs = "-m waitress --host=0.0.0.0 --port=$HubPort wsgi:application"
    }

    # Retire the legacy scheduled task if this box predates the service, so the two don't
    # both bind the port.
    if (Get-ScheduledTask -TaskName $TaskHub -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskHub -Confirm:$false
        Ok "Removed legacy scheduled task: $TaskHub"
    }
    # Reinstall cleanly if a prior service is already registered.
    if (Remove-HubService $hubDir) { Say "Reconfiguring existing service." }

    $wrapperExe = Join-Path $hubDir "$HubServiceId.exe"
    $wrapperXml = Join-Path $hubDir "$HubServiceId.xml"
    if (-not (Test-Path $wrapperExe)) {
        Say "Downloading WinSW service wrapper..."
        try { Invoke-WebRequest -Uri $WinSwUrl -OutFile $wrapperExe -UseBasicParsing }
        catch { Die "Could not download the WinSW service wrapper from $WinSwUrl -- $($_.Exception.Message)" }
        Ok "Wrapper: $wrapperExe"
    }

    # WinSW runs as LocalSystem by default (matches the old SYSTEM task). The service inherits
    # config from .env because waitress runs with <workingdirectory>=$hubDir and app.py calls
    # load_dotenv() from cwd. onfailure=restart is also what the hub self-update relies on:
    # restart_hub() exits non-zero, WinSW relaunches within ~5s.
    $xml = @"
<service>
  <id>$HubServiceId</id>
  <name>$HubServiceName</name>
  <description>Temp Monitor hub (Flask/Socket.IO via waitress).</description>
  <executable>$([System.Security.SecurityElement]::Escape($hubExec))</executable>
  <arguments>$([System.Security.SecurityElement]::Escape($hubArgs))</arguments>
  <workingdirectory>$([System.Security.SecurityElement]::Escape($hubDir))</workingdirectory>
  <startmode>Automatic</startmode>
  <onfailure action="restart" delay="5 sec"/>
  <resetfailure>1 hour</resetfailure>
  <log mode="roll-by-size">
    <sizeThreshold>10240</sizeThreshold>
    <keepFiles>5</keepFiles>
  </log>
</service>
"@
    Set-Content -Path $wrapperXml -Value $xml -Encoding UTF8
    Ok "Wrote $wrapperXml"

    & $wrapperExe install
    if ($LASTEXITCODE -ne 0) { Die "Service install failed (WinSW exit $LASTEXITCODE)." }
    Start-Service -Name $HubServiceId
    Ok "Service '$HubServiceName' installed and started (LocalSystem, Automatic)"

    Step "Starting hub"
    Say "Waiting for the hub to respond..."
    $live = $false
    foreach ($i in 1..20) {
        Start-Sleep -Seconds 1
        try {
            Invoke-WebRequest -Uri "http://localhost:$HubPort/" -UseBasicParsing -TimeoutSec 2 | Out-Null
            $live = $true
            break
        } catch {
            if ($_.Exception.Response) { $live = $true; break }
        }
    }
    if ($live) { Ok "Hub responding on http://localhost:$HubPort/" }
    else { Warn "No response yet on port $HubPort -- check 'Get-Service $HubServiceId' and $hubDir\$HubServiceId.wrapper.log." }

    Write-Host @"

  Done.

  Hub URL (local) : http://localhost:$HubPort/
  Hub URL (public): $hubUrlValue
  Location        : $hubDir
  Config          : $envPath
  Service         : $HubServiceName  (Get-Service $HubServiceId)

  Uninstall: powershell -ExecutionPolicy Bypass -File install.ps1 -Component Hub -Uninstall

"@ -ForegroundColor Green
}

# ----------------------------------------------------------------------
# Resolve which component to act on, then dispatch
# ----------------------------------------------------------------------
if (-not $Component) {
    if ($Uninstall) {
        # Back-compat: bare -Uninstall (no -Component) matches the documented
        # legacy behavior of uninstalling the companion.
        $Component = "Companion"
    } else {
        $Component = Show-Menu
        if ($Component -eq "UninstallMenu") {
            $Component = Show-UninstallMenu
            $Uninstall = $true
        }
    }
}

switch ($Component) {
    "Agent"     { if ($Uninstall) { Uninstall-Agent }     else { Install-Agent } }
    "Companion" { if ($Uninstall) { Uninstall-Companion } else { Install-Companion } }
    "Hub"       { if ($Uninstall) { Uninstall-Hub }       else { Install-Hub } }
}
